"""Add raster tile layers to a canyon GeoPackage, covering its canyons plus a buffer.

Layers (each its own tile table, so they can be switched and blended in the viewer):

* ``tiles_osm``       OSM map, zoom 13-18: rendered here with ``--topo`` (hillshade, forest
                      shading, water, roads and trails, peaks), or taken from ``--osm-mbtiles``
* ``tiles_imagery``   aerial imagery (e.g. NAIP), zoom 14-18, from a GeoTIFF
* ``tiles_hillshade`` hillshade from a 1 m DEM, zoom 14-17
* ``tiles_contours_m`` / ``tiles_contours_ft``  contour lines with elevation labels (``--topo``),
                      zoom 14-17, one overlay per unit so the viewer can follow its setting
* ``tiles_terrain``   elevation for the viewer's 3D map (``--terrain``): Terrarium-encoded PNG,
                      zoom 11-16, from the DEM. Data, not a picture: the viewer does not list it
                      as a layer. ``--terrain`` alone adds it to a file that already has tiles.

Imagery and DEM can be local files (``--imagery``, ``--dem``) or downloaded for the canyon
area (``--fetch-naip``, ``--fetch-dem``). Downloads go to ``tile_cache`` beside the GeoPackage.

Usage (inside ``nix develop``)::

    canyongis-gpkg-tiles runs/canyons.gpkg --fetch-naip --fetch-dem --dry-run
    # imagery, hillshade and the offline topo map with contours (needs network for OSM):
    canyongis-gpkg-tiles runs/canyons.gpkg --fetch-naip --fetch-dem --topo
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .geometry import blob_envelope
from .schema import open_geopackage
from .tiles import (
    ZOOMS_CONTOURS,
    ZOOMS_HILLSHADE,
    ZOOMS_IMAGERY,
    ZOOMS_MAP,
    ZOOMS_TERRAIN,
    buffer_bbox_lonlat,
    count_tiles,
    write_tile_table,
)

DEFAULT_BUFFER_M = 500.0
# Guard against an accidental planet-sized build. Override with --max-tiles.
DEFAULT_MAX_TILES = 200_000
_GEOMETRY_TABLES = ("canyons", "routes", "waypoints", "rappels")
ATTRIBUTION = {
    "osm": "© OpenStreetMap contributors, ODbL",
    "imagery": "USDA NAIP, public domain",
    "hillshade": "Hillshade from USGS 1 m DEM, public domain",
    "contours": "Contours from USGS 1 m DEM, public domain",
    "terrain": "Elevation from USGS 1 m DEM, public domain",
}
CONTOUR_LAYERS = {"contours_m": "m", "contours_ft": "ft"}


def canyon_bbox_lonlat(conn: sqlite3.Connection) -> tuple[float, float, float, float]:
    """WGS84 bbox ``(west, south, east, north)`` of every geometry in the canyon tables."""
    boxes = []
    for table in _GEOMETRY_TABLES:
        for (blob,) in conn.execute(f"SELECT geom FROM {table} WHERE geom IS NOT NULL"):
            boxes.append(blob_envelope(blob))
    if not boxes:
        raise ValueError("GeoPackage has no geometry to cover")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def plan(bbox: tuple[float, float, float, float], layers: list[str]) -> dict[str, dict[int, int]]:
    zooms = {
        "osm": ZOOMS_MAP,
        "imagery": ZOOMS_IMAGERY,
        "hillshade": ZOOMS_HILLSHADE,
        "contours_m": ZOOMS_CONTOURS,
        "contours_ft": ZOOMS_CONTOURS,
        "terrain": ZOOMS_TERRAIN,
    }
    return {layer: count_tiles(bbox, zooms[layer]) for layer in layers}


def build_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="canyongis-gpkg-tiles", description="Add raster tile layers to a canyon GeoPackage."
    )
    parser.add_argument("gpkg", type=Path)
    parser.add_argument("--osm-mbtiles", type=Path, help="rendered OSM raster MBTiles")
    parser.add_argument(
        "--topo",
        action="store_true",
        help="render the offline topo map from OSM (Overpass), the DEM and NAIP: "
        "tiles_osm plus metre and foot contour overlays; needs --dem/--fetch-dem",
    )
    parser.add_argument("--imagery", type=Path, help="local aerial imagery GeoTIFF")
    parser.add_argument(
        "--fetch-naip",
        action="store_true",
        help="download NAIP scenes covering the canyons (Planetary Computer, free, no key)",
    )
    parser.add_argument("--dem", type=Path, help="local 1 m DEM GeoTIFF in a projected CRS")
    parser.add_argument(
        "--fetch-dem",
        action="store_true",
        help="download USGS 1 m DEM tiles covering the canyons (TNM)",
    )
    parser.add_argument(
        "--terrain",
        action="store_true",
        help="add the elevation tile layer (tiles_terrain) the viewer's 3D map needs; "
        "needs --dem/--fetch-dem",
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="downloads (default: tile_cache beside the GeoPackage)"
    )
    parser.add_argument(
        "--dem-source-dir",
        type=Path,
        help="where the raw 1 m DEM tiles are kept (default: the cache dir); shared between "
        "builds, so canyons that are close together download them once",
    )
    parser.add_argument("--buffer-m", type=float, default=DEFAULT_BUFFER_M)
    parser.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    parser.add_argument(
        "--dry-run", action="store_true", help="count tiles and list downloads, write nothing"
    )
    args = parser.parse_args(argv)

    imagery_source = args.imagery or (args.fetch_naip and "naip")
    dem_source = args.dem or (args.fetch_dem and "tnm")
    if args.topo and args.osm_mbtiles:
        parser.error("--topo and --osm-mbtiles both write tiles_osm; give one")
    if args.topo and not dem_source:
        parser.error("--topo needs a DEM for contours and relief: add --dem or --fetch-dem")
    if args.terrain and not dem_source:
        parser.error("--terrain needs a DEM for elevation: add --dem or --fetch-dem")
    layers = []
    if args.osm_mbtiles or args.topo:
        layers.append("osm")
    if imagery_source:
        layers.append("imagery")
    # --terrain alone (with a DEM) only adds elevation, e.g. to a file that already has its tiles
    terrain_only = args.terrain and not (args.topo or imagery_source or args.osm_mbtiles)
    if dem_source and not terrain_only:
        layers.append("hillshade")
    if args.topo:
        layers += list(CONTOUR_LAYERS)
    if args.terrain:
        layers.append("terrain")
    if not layers:
        parser.error(
            "give at least one of --osm-mbtiles, --topo, --terrain, --imagery/--fetch-naip, "
            "--dem/--fetch-dem"
        )

    cache = args.cache_dir or args.gpkg.parent / "tile_cache"
    try:
        conn = open_geopackage(args.gpkg)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"{exc}\nbuild it first with canyongis-ropewiki-gpkg")
    bbox = buffer_bbox_lonlat(canyon_bbox_lonlat(conn), args.buffer_m)
    counts = plan(bbox, layers)
    total = 0
    for layer, per_zoom in counts.items():
        layer_total = sum(per_zoom.values())
        total += layer_total
        detail = ", ".join(f"z{z}: {n}" for z, n in sorted(per_zoom.items()))
        print(f"{layer:10} {layer_total:7} tiles  ({detail})")
    print(f"bbox (lon/lat): {tuple(round(v, 5) for v in bbox)}; total {total} tiles")

    from . import data_fetch, tile_sources

    def ensure_dem() -> Path:
        """The DEM mosaic (downloaded once into the cache when --fetch-dem)."""
        dem = args.dem
        if dem_source == "tnm":
            dem = cache / "dem_mosaic.tif"
            if not dem.exists():
                products = data_fetch.dem_products(bbox)
                paths = data_fetch.download_dem_tiles(products, args.dem_source_dir or cache)
                data_fetch.build_dem_mosaic(paths, bbox, dem)
                # which raw tiles the cropped DEM was cut from, for whoever keeps it (and cleans up)
                (cache / "dem_sources.json").write_text(json.dumps([str(p) for p in paths]))
        return dem

    def ensure_imagery() -> Path:
        imagery = args.imagery
        if imagery_source == "naip":
            imagery = cache / "naip_mosaic.tif"
            if not imagery.exists():
                data_fetch.build_naip_mosaic(bbox, imagery)
        return imagery

    dem_products = None
    if args.dry_run:
        if imagery_source == "naip":
            scenes = data_fetch.latest_per_quad(data_fetch.naip_scenes(bbox))
            print(f"NAIP: {len(scenes)} scenes: " + ", ".join(s["id"] for s in scenes))
        if dem_source == "tnm":
            dem_products = data_fetch.dem_products(bbox)
            print(
                f"DEM: {len(dem_products)} TNM tiles, "
                f"{data_fetch.total_bytes(dem_products) / 1e9:.2f} GB to download"
            )
        if args.topo:
            print("OSM: roads, trails, water and forest from Overpass (cached in the tile cache)")
        return
    if total > args.max_tiles:
        sys.exit(
            f"refusing to write {total} tiles (limit {args.max_tiles}); "
            "raise --max-tiles if this is intended"
        )

    if args.osm_mbtiles:
        render = tile_sources.mbtiles_source(args.osm_mbtiles)
        n = write_tile_table(
            conn,
            "tiles_osm",
            identifier="OSM map",
            description="OpenStreetMap raster",
            bbox_lonlat=bbox,
            zooms=ZOOMS_MAP,
            render=render,
            attribution=ATTRIBUTION["osm"],
        )
        print(f"tiles_osm: wrote {sum(n.values())} tiles")
    imagery = None
    if "imagery" in layers:
        imagery = ensure_imagery()
        render = tile_sources.raster_source(imagery, bands=(1, 2, 3))
        n = write_tile_table(
            conn,
            "tiles_imagery",
            identifier="Imagery",
            description="Aerial imagery",
            bbox_lonlat=bbox,
            zooms=ZOOMS_IMAGERY,
            render=render,
            attribution=ATTRIBUTION["imagery"],
        )
        print(f"tiles_imagery: wrote {sum(n.values())} tiles")
    shade = None
    if "hillshade" in layers or args.topo:
        dem = ensure_dem()
        cache.mkdir(parents=True, exist_ok=True)
        shade = tile_sources.hillshade_geotiff(dem, cache / "hillshade.tif")
    if "hillshade" in layers:
        render = tile_sources.raster_source(shade, bands=(1,), alpha_band=2)
        n = write_tile_table(
            conn,
            "tiles_hillshade",
            identifier="Hillshade",
            description="Hillshade",
            bbox_lonlat=bbox,
            zooms=ZOOMS_HILLSHADE,
            render=render,
            attribution=ATTRIBUTION["hillshade"],
        )
        print(f"tiles_hillshade: wrote {sum(n.values())} tiles")
    if "terrain" in layers:
        n = write_tile_table(
            conn,
            "tiles_terrain",
            identifier="Elevation",
            description="Terrain elevation (Terrarium PNG)",
            bbox_lonlat=bbox,
            zooms=ZOOMS_TERRAIN,
            render=tile_sources.terrain_source(ensure_dem()),
            attribution=ATTRIBUTION["terrain"],
        )
        print(f"tiles_terrain: wrote {sum(n.values())} tiles")
    if args.topo:
        renderer = tile_sources.topo_renderer(bbox, cache, ensure_dem(), shade, imagery)
        n = write_tile_table(
            conn,
            "tiles_osm",
            identifier="Topo map",
            description="OSM topo map: relief, forest, water, roads and trails",
            bbox_lonlat=bbox,
            zooms=ZOOMS_MAP,
            render=renderer.base,
            attribution=ATTRIBUTION["osm"] + "; relief from USGS 1 m DEM; forest from USDA NAIP",
        )
        print(f"tiles_osm: wrote {sum(n.values())} tiles")
        for table, unit in CONTOUR_LAYERS.items():
            n = write_tile_table(
                conn,
                f"tiles_{table}",
                identifier=f"Contours ({unit})",
                description=f"Contour lines and labels in {unit}",
                bbox_lonlat=bbox,
                zooms=ZOOMS_CONTOURS,
                render=lambda z, x, y, u=unit: renderer.contour_tile(u, z, x, y),
                attribution=ATTRIBUTION["contours"],
            )
            print(f"tiles_{table}: wrote {sum(n.values())} tiles")
    conn.close()


if __name__ == "__main__":
    build_cli()

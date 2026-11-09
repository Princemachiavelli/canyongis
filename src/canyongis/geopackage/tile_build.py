"""Add raster tile layers to a canyon GeoPackage, covering its canyons plus a buffer.

Layers (each its own tile table, so they can be switched and blended in the viewer):

* ``tiles_osm``       OSM map, zoom 13-18, from an MBTiles file rendered from OSM data
* ``tiles_imagery``   aerial imagery (e.g. NAIP), zoom 14-18, from a GeoTIFF
* ``tiles_hillshade`` hillshade from a 1 m DEM, zoom 14-17

Imagery and DEM can be local files (``--imagery``, ``--dem``) or downloaded for the canyon
area (``--fetch-naip``, ``--fetch-dem``). Downloads go to ``tile_cache`` beside the GeoPackage.

Usage (inside ``nix develop``)::

    canyongis-gpkg-tiles runs/canyons.gpkg --fetch-naip --fetch-dem --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .geometry import blob_envelope
from .tiles import (
    ZOOMS_HILLSHADE,
    ZOOMS_IMAGERY,
    ZOOMS_MAP,
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
}


def canyon_bbox_lonlat(conn: sqlite3.Connection) -> tuple[float, float, float, float]:
    """WGS84 bbox ``(west, south, east, north)`` of every geometry in the canyon tables."""
    boxes = []
    for table in _GEOMETRY_TABLES:
        for (blob,) in conn.execute(f"SELECT geom FROM {table} WHERE geom IS NOT NULL"):
            boxes.append(blob_envelope(blob))
    if not boxes:
        raise ValueError("GeoPackage has no geometry to cover")
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def plan(bbox: tuple[float, float, float, float], layers: list[str]) -> dict[str, dict[int, int]]:
    zooms = {"osm": ZOOMS_MAP, "imagery": ZOOMS_IMAGERY, "hillshade": ZOOMS_HILLSHADE}
    return {layer: count_tiles(bbox, zooms[layer]) for layer in layers}


def build_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="canyongis-gpkg-tiles",
                                     description="Add raster tile layers to a canyon GeoPackage.")
    parser.add_argument("gpkg", type=Path)
    parser.add_argument("--osm-mbtiles", type=Path, help="rendered OSM raster MBTiles")
    parser.add_argument("--imagery", type=Path, help="local aerial imagery GeoTIFF")
    parser.add_argument("--fetch-naip", action="store_true",
                        help="download NAIP scenes covering the canyons (AWS, no key needed)")
    parser.add_argument("--dem", type=Path, help="local 1 m DEM GeoTIFF in a projected CRS")
    parser.add_argument("--fetch-dem", action="store_true",
                        help="download USGS 1 m DEM tiles covering the canyons (TNM)")
    parser.add_argument("--cache-dir", type=Path, help="downloads (default: tile_cache beside the GeoPackage)")
    parser.add_argument("--buffer-m", type=float, default=DEFAULT_BUFFER_M)
    parser.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    parser.add_argument("--dry-run", action="store_true",
                        help="count tiles and list downloads, write nothing")
    args = parser.parse_args(argv)

    imagery_source = args.imagery or (args.fetch_naip and "naip")
    dem_source = args.dem or (args.fetch_dem and "tnm")
    layers = []
    if args.osm_mbtiles:
        layers.append("osm")
    if imagery_source:
        layers.append("imagery")
    if dem_source:
        layers.append("hillshade")
    if not layers:
        parser.error("give at least one of --osm-mbtiles, --imagery/--fetch-naip, --dem/--fetch-dem")

    cache = args.cache_dir or args.gpkg.parent / "tile_cache"
    conn = sqlite3.connect(args.gpkg)
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

    dem_products = None
    if args.dry_run:
        if imagery_source == "naip":
            scenes = data_fetch.latest_per_quad(data_fetch.naip_scenes(bbox))
            print(f"NAIP: {len(scenes)} scenes: " + ", ".join(s["id"] for s in scenes))
        if dem_source == "tnm":
            dem_products = data_fetch.dem_products(bbox)
            print(f"DEM: {len(dem_products)} TNM tiles, "
                  f"{data_fetch.total_bytes(dem_products) / 1e9:.2f} GB to download")
        return
    if total > args.max_tiles:
        sys.exit(f"refusing to write {total} tiles (limit {args.max_tiles}); "
                 "raise --max-tiles if this is intended")

    if "osm" in layers:
        render = tile_sources.mbtiles_source(args.osm_mbtiles)
        n = write_tile_table(conn, "tiles_osm", identifier="OSM map",
                             description="OpenStreetMap raster", bbox_lonlat=bbox,
                             zooms=ZOOMS_MAP, render=render, attribution=ATTRIBUTION["osm"])
        print(f"tiles_osm: wrote {sum(n.values())} tiles")
    if "imagery" in layers:
        imagery = args.imagery
        if imagery_source == "naip":
            imagery = cache / "naip_mosaic.tif"
            if not imagery.exists():
                data_fetch.build_naip_mosaic(bbox, imagery)
        render = tile_sources.raster_source(imagery, bands=(1, 2, 3))
        n = write_tile_table(conn, "tiles_imagery", identifier="Imagery",
                             description="Aerial imagery", bbox_lonlat=bbox,
                             zooms=ZOOMS_IMAGERY, render=render,
                             attribution=ATTRIBUTION["imagery"])
        print(f"tiles_imagery: wrote {sum(n.values())} tiles")
    if "hillshade" in layers:
        dem = args.dem
        if dem_source == "tnm":
            dem = cache / "dem_mosaic.tif"
            if not dem.exists():
                products = data_fetch.dem_products(bbox)
                paths = data_fetch.download_dem_tiles(products, cache)
                data_fetch.build_dem_mosaic(paths, bbox, dem)
        cache.mkdir(parents=True, exist_ok=True)
        shade = tile_sources.hillshade_geotiff(dem, cache / "hillshade.tif")
        render = tile_sources.raster_source(shade, bands=(1,), alpha_band=2)
        n = write_tile_table(conn, "tiles_hillshade", identifier="Hillshade",
                             description="Hillshade", bbox_lonlat=bbox,
                             zooms=ZOOMS_HILLSHADE, render=render,
                             attribution=ATTRIBUTION["hillshade"])
        print(f"tiles_hillshade: wrote {sum(n.values())} tiles")
    conn.close()


if __name__ == "__main__":
    build_cli()

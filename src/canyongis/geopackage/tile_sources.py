"""Tile renderers for ``tiles.write_tile_table``.

* ``mbtiles_source``: pre-rendered raster tiles in an MBTiles file (the OSM map, once a
  renderer has produced one). Pure SQLite.
* ``raster_source``: reprojects a GeoTIFF (imagery, or hillshade) into Web Mercator tiles.
  Needs rasterio and Pillow, which come from the Nix environment.
* ``topo_renderer``: the offline topo map and contour renderer (see ``topo``).
* ``hillshade_geotiff``: hillshade GeoTIFF from a DEM, with a coverage band, using the
  canyongis hillshade function.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

from .tiles import TILE_SIZE, Render, tile_bounds_3857


def mbtiles_source(path: str | Path) -> Render:
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)

    def render(zoom: int, x: int, y: int) -> bytes | None:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (zoom, x, (1 << zoom) - 1 - y),
        ).fetchone()
        return bytes(row[0]) if row else None

    return render


def raster_source(
    path: str | Path,
    *,
    bands: tuple[int, ...] = (1, 2, 3),
    alpha_band: int | None = None,
    jpeg_quality: int = 90,
) -> Render:
    """Render Web Mercator tiles from a GeoTIFF.

    Colour tiles are JPEG where the tile is fully covered by the source and PNG (RGBA)
    at edges. With ``alpha_band`` set, that band is used as the alpha channel instead of
    the coverage mask, and the output is always PNG. Tiles with no coverage return None.
    """
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    src = rasterio.open(path)
    coverage = np.ones((src.height, src.width), dtype=np.uint8)

    def render(zoom: int, x: int, y: int) -> bytes | None:
        dst_transform = from_bounds(*tile_bounds_3857(zoom, x, y), TILE_SIZE, TILE_SIZE)
        planes = np.zeros((len(bands), TILE_SIZE, TILE_SIZE), dtype=np.uint8)
        for i, band in enumerate(bands):
            reproject(
                source=rasterio.band(src, band),
                destination=planes[i],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.bilinear,
            )
        if alpha_band is not None:
            alpha = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
            reproject(
                source=rasterio.band(src, alpha_band),
                destination=alpha,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.nearest,
            )
        else:
            alpha = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
            reproject(
                source=coverage,
                destination=alpha,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.nearest,
            )
            alpha = np.where(alpha > 0, 255, 0).astype(np.uint8)
        if not alpha.any():
            return None
        out = io.BytesIO()
        if alpha_band is None and len(bands) == 3 and alpha.all():
            Image.fromarray(np.moveaxis(planes, 0, -1), "RGB").save(
                out, "JPEG", quality=jpeg_quality
            )
        elif len(bands) == 1:
            img = Image.fromarray(planes[0], "L")
            img.putalpha(Image.fromarray(alpha, "L"))
            img.save(out, "PNG")
        else:
            rgba = np.dstack([np.moveaxis(planes[:3], 0, -1), alpha])
            Image.fromarray(rgba, "RGBA").save(out, "PNG")
        return out.getvalue()

    return render


# Terrarium encoding (as used by Mapzen / AWS terrain tiles and read by MapLibre's raster-dem
# source): metres = (R * 256 + G + B / 256) - 32768, in a lossless PNG. JPEG or any resampling of
# the encoded colours would corrupt the elevation, so these tiles are only ever PNG.
TERRARIUM_OFFSET = 32768.0


def encode_terrarium(elevation_m: Any) -> bytes:
    """A 256x256 float array of metres as a Terrarium PNG."""
    import numpy as np
    from PIL import Image

    value = np.clip(elevation_m.astype(np.float64) + TERRARIUM_OFFSET, 0.0, 65535.99)
    red = np.floor(value / 256.0)
    green = np.floor(value - red * 256.0)
    blue = np.floor((value - np.floor(value)) * 256.0)
    rgb = np.stack([red, green, blue], axis=-1).astype(np.uint8)
    out = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(out, "PNG")
    return out.getvalue()


def decode_terrarium(png: bytes) -> Any:
    """Metres from a Terrarium PNG (the inverse of ``encode_terrarium``)."""
    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64)
    return (rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - TERRARIUM_OFFSET).astype(
        np.float32
    )


def terrain_source(path: str | Path) -> Render:
    """Render Web Mercator Terrarium tiles from a DEM GeoTIFF (elevations in metres).

    Elevations are resampled bilinearly in floating point. Pixels the DEM does not cover are
    filled from the nearest covered pixel (a hole left at 0 m would be a cliff in 3D); tiles the
    DEM does not touch at all return None.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject
    from scipy import ndimage

    src = rasterio.open(path)

    def render(zoom: int, x: int, y: int) -> bytes | None:
        dst = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=from_bounds(*tile_bounds_3857(zoom, x, y), TILE_SIZE, TILE_SIZE),
            dst_crs="EPSG:3857",
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        covered = np.isfinite(dst)
        if not covered.any():
            return None
        if not covered.all():
            nearest = ndimage.distance_transform_edt(
                ~covered, return_distances=False, return_indices=True
            )
            dst = dst[tuple(nearest)]
        return encode_terrarium(dst)

    return render


def hillshade_geotiff(dem_path: str | Path, out_path: str | Path) -> Path:
    """Write a 2-band GeoTIFF from a DEM: band 1 hillshade (0-255), band 2 coverage (0/255)."""
    import numpy as np
    import rasterio

    from ..ravine_compare import compute_hillshade

    with rasterio.open(dem_path) as src:
        elev = src.read(1).astype(np.float32)
        nodata = src.nodata
        valid = np.isfinite(elev)
        if nodata is not None:
            valid &= elev != nodata
        filled = np.where(valid, elev, 0.0).astype(np.float32)
        shade = compute_hillshade(filled, float(src.res[0]))
        profile = src.profile.copy()
        profile.update(count=2, dtype="uint8", nodata=None, compress="deflate", driver="GTiff")
        coverage = np.where(valid, 255, 0).astype(np.uint8)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(shade.astype(np.uint8), 1)
            dst.write(coverage, 2)
    return Path(out_path)


def topo_renderer(bbox_lonlat, cache_dir, dem_path, hillshade_path, imagery_path=None):
    """A ``topo.TopoRenderer`` for the bbox: OSM data (downloaded and cached), contours in metres
    and feet from the DEM, relief from the hillshade and, with 4-band NAIP imagery, forest cover."""
    from ..io import log
    from . import topo

    log("topo: fetching OSM data")
    osm = topo.load_osm(topo.fetch_osm(bbox_lonlat, cache_dir))
    log(f"topo: {len(osm.lines)} lines, {len(osm.areas)} areas, {len(osm.points)} points")
    contours = {}
    for unit in ("m", "ft"):
        log(f"topo: contours ({unit})")
        contours[unit] = topo.build_contours(dem_path, unit)
    vegetation = None
    if imagery_path is not None:
        vegetation = topo.build_vegetation(imagery_path, cache_dir / "vegetation.tif")
        log("topo: forest shading from NAIP" if vegetation else "topo: imagery has no NIR band")
    return topo.TopoRenderer(osm, contours, hillshade=hillshade_path, vegetation=vegetation)

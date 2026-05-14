from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds


@dataclass(frozen=True)
class TileArchive:
    pmtiles_path: str
    bounds_wgs84: tuple[float, float, float, float]
    min_zoom: int
    max_zoom: int
    tile_format: str = "webp"
    tile_archive_format: str = "pmtiles"


def bounds_wgs84_for_grid(
    height: int,
    width: int,
    transform,
    crs,
) -> tuple[float, float, float, float]:
    west, south, east, north = array_bounds(height, width, transform)
    return tuple(
        float(v)
        for v in transform_bounds(crs, "EPSG:4326", west, south, east, north, densify_pts=21)
    )


def _estimate_zoom(width: int, height: int, bounds_wgs84: tuple[float, float, float, float]) -> int:
    west, south, east, north = bounds_wgs84
    lon_span = max(abs(east - west), 1e-9)
    lat_span = max(abs(north - south), 1e-9)
    x_zoom = math.log2(max(width, 1) * 360.0 / (256.0 * lon_span))
    mid_lat = max(min((north + south) / 2.0, 85.0), -85.0)
    merc_lat_factor = max(math.cos(math.radians(mid_lat)), 0.05)
    y_zoom = math.log2(max(height, 1) * 360.0 * merc_lat_factor / (256.0 * lat_span))
    return max(0, min(22, round(max(x_zoom, y_zoom))))


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required tiling tool not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
        raise RuntimeError(f"{cmd[0]} failed: {output}") from exc


def _write_rgba_geotiff(
    path: Path,
    image: Image.Image,
    valid_mask: np.ndarray,
    transform,
    crs,
) -> None:
    rgba = _rgba_array(image, valid_mask)
    profile = {
        "driver": "GTiff",
        "height": rgba.shape[0],
        "width": rgba.shape[1],
        "count": 4,
        "dtype": "uint8",
        "crs": crs,
        "transform": transform,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        for band in range(4):
            dst.write(rgba[:, :, band], band + 1)
        dst.colorinterp = (
            rasterio.enums.ColorInterp.red,
            rasterio.enums.ColorInterp.green,
            rasterio.enums.ColorInterp.blue,
            rasterio.enums.ColorInterp.alpha,
        )


def _rgba_array(image: Image.Image, valid_mask: np.ndarray) -> np.ndarray:
    rgba = np.asarray(image.convert("RGBA")).copy()
    if valid_mask.shape != rgba.shape[:2]:
        raise ValueError("valid_mask shape must match preview image")
    rgba[:, :, 3] = np.where(valid_mask, rgba[:, :, 3], 0).astype(np.uint8)
    return rgba


def _downsample_by_factor(image: Image.Image, factor: float) -> Image.Image:
    if factor <= 1:
        return image
    new_size = (
        max(1, round(image.size[0] / factor)),
        max(1, round(image.size[1] / factor)),
    )
    return image.resize(new_size, Image.LANCZOS)


def write_wgs84_webp_preview(
    image: Image.Image,
    valid_mask: np.ndarray,
    transform,
    crs,
    out_path: str | Path,
    *,
    quality: int = 85,
    downsample_factor: float = 1,
) -> tuple[float, float, float, float]:
    rgba = _rgba_array(image, valid_mask)
    height, width = rgba.shape[:2]
    src_bounds = array_bounds(height, width, transform)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        crs,
        "EPSG:4326",
        width,
        height,
        *src_bounds,
    )
    dst = np.zeros((dst_height, dst_width, 4), dtype=np.uint8)
    for band in range(4):
        reproject(
            source=rgba[:, :, band],
            destination=dst[:, :, band],
            src_transform=transform,
            src_crs=crs,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.nearest if band == 3 else Resampling.bilinear,
        )
    west, south, east, north = array_bounds(dst_height, dst_width, dst_transform)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _downsample_by_factor(Image.fromarray(dst, mode="RGBA"), downsample_factor).save(
        out_path, quality=quality, method=4
    )
    return (float(west), float(south), float(east), float(north))


def write_pmtiles_preview(
    image: Image.Image,
    valid_mask: np.ndarray,
    transform,
    crs,
    out_path: str | Path,
    *,
    quality: int = 85,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
    keep_intermediates: bool = False,
) -> TileArchive:
    out_path = Path(out_path)
    work_dir = out_path.with_suffix("")
    work_dir.mkdir(parents=True, exist_ok=True)
    source_tif = work_dir / "source_rgba.tif"
    mercator_tif = work_dir / "webmercator_rgba.tif"
    mbtiles_path = work_dir / "tiles.mbtiles"
    bounds_wgs84 = bounds_wgs84_for_grid(image.height, image.width, transform, crs)
    native_zoom = _estimate_zoom(image.width, image.height, bounds_wgs84)
    max_zoom = native_zoom if max_zoom is None else max_zoom
    min_zoom = max(0, max_zoom - 4) if min_zoom is None else min_zoom

    _write_rgba_geotiff(source_tif, image, valid_mask, transform, crs)
    _run(
        [
            "gdalwarp",
            "-t_srs",
            "EPSG:3857",
            "-r",
            "bilinear",
            "-dstalpha",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(source_tif),
            str(mercator_tif),
        ]
    )
    _run(
        [
            "gdal_translate",
            "-of",
            "MBTILES",
            "-co",
            "TILE_FORMAT=WEBP",
            "-co",
            f"QUALITY={quality}",
            "-co",
            "ZOOM_LEVEL_STRATEGY=UPPER",
            str(mercator_tif),
            str(mbtiles_path),
        ]
    )
    overview_levels = [str(2**i) for i in range(1, max(1, max_zoom - min_zoom) + 1)]
    if overview_levels:
        _run(["gdaladdo", "-r", "average", str(mbtiles_path), *overview_levels])
    _run(["pmtiles", "convert", str(mbtiles_path), str(out_path)])
    metadata_path = out_path.with_suffix(".pmtiles.json")
    metadata_path.write_text(
        json.dumps(asdict(TileArchive(str(out_path), bounds_wgs84, min_zoom, max_zoom)), indent=2)
        + "\n"
    )
    if not keep_intermediates:
        shutil.rmtree(work_dir, ignore_errors=True)
    return TileArchive(str(out_path), bounds_wgs84, min_zoom, max_zoom)

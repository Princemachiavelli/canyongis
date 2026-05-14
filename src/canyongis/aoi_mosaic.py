from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

from .ingest import _download_file, _item_download_url, _purge_part_files, query_tnm_products
from .io import log


@dataclass(frozen=True)
class AOIMosaicResult:
    dem_path: str
    source_paths: list[str]
    bounds_wgs84: tuple[float, float, float, float]
    crs: str
    pixel_size_m: float
    width: int
    height: int


def _inflate_wgs84_bounds(
    bounds: tuple[float, float, float, float], buffer_m: float
) -> tuple[float, float, float, float]:
    if buffer_m <= 0:
        return bounds
    west, south, east, north = bounds
    mean_lat_rad = math.radians(0.5 * (south + north))
    dlat = buffer_m / 111_000.0
    dlon = buffer_m / (111_000.0 * max(math.cos(mean_lat_rad), 1e-6))
    return (west - dlon, south - dlat, east + dlon, north + dlat)


def _safe_download_name(url: str, fallback: str) -> str:
    name = Path(urlparse(url).path).name
    if not name:
        name = fallback
    if not name.lower().endswith((".tif", ".tiff")):
        name = f"{Path(name).stem}.tif"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def _download_tnm_products(
    bounds_wgs84: tuple[float, float, float, float],
    cache_dir: Path,
    *,
    max_products: int,
) -> list[Path]:
    products = query_tnm_products(bounds_wgs84)
    cache_dir.mkdir(parents=True, exist_ok=True)
    purged = _purge_part_files(cache_dir)
    if purged:
        log(f"purged {purged} stale .part file(s) in {cache_dir}")
    paths: list[Path] = []
    for idx, item in enumerate(products[:max_products]):
        url = _item_download_url(item)
        if not url:
            continue
        path = cache_dir / _safe_download_name(url, f"tnm_dem_{idx}.tif")
        if not path.exists():
            log(f"downloading TNM DEM product: {url}")
            _download_file(url, path)
        paths.append(path)
    if not paths:
        raise RuntimeError("TNM returned no downloadable DEM products for AOI")
    return paths


def build_aoi_dem_mosaic(
    bounds_wgs84: tuple[float, float, float, float],
    out_path: str | Path,
    *,
    cache_dir: str | Path,
    buffer_m: float = 0.0,
    max_products: int = 24,
) -> AOIMosaicResult:
    """Download cached TNM DEM products for an AOI and write a cropped mosaic.

    This is intentionally AOI/bbox-oriented. It complements the existing
    Ropewiki/KML mosaic code, which groups around descent tracks.
    """
    out_path = Path(out_path)
    cache_dir = Path(cache_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buffered_bounds = _inflate_wgs84_bounds(bounds_wgs84, buffer_m)

    source_paths = _download_tnm_products(buffered_bounds, cache_dir, max_products=max_products)
    datasets = [rasterio.open(path) for path in source_paths]
    vrts: list[WarpedVRT] = []
    try:
        target_crs = datasets[0].crs
        if target_crs is None:
            raise ValueError(f"source DEM has no CRS: {source_paths[0]}")
        target_bounds = transform_bounds("EPSG:4326", target_crs, *buffered_bounds, densify_pts=21)
        sources = []
        for src in datasets:
            if src.crs is None:
                raise ValueError(f"source DEM has no CRS: {src.name}")
            if src.crs == target_crs:
                sources.append(src)
            else:
                vrt = WarpedVRT(src, crs=target_crs)
                vrts.append(vrt)
                sources.append(vrt)

        mosaic, transform = merge(sources, bounds=target_bounds, nodata=-9999.0)
        arr = mosaic[0].astype(np.float32, copy=False)
        profile = datasets[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=arr.shape[0],
            width=arr.shape[1],
            count=1,
            transform=transform,
            crs=target_crs,
            dtype="float32",
            nodata=-9999.0,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(arr, 1)

        pixel_size = abs(float(transform.a))
        return AOIMosaicResult(
            dem_path=str(out_path),
            source_paths=[str(path) for path in source_paths],
            bounds_wgs84=buffered_bounds,
            crs=str(target_crs),
            pixel_size_m=pixel_size,
            width=int(arr.shape[1]),
            height=int(arr.shape[0]),
        )
    finally:
        for vrt in vrts:
            vrt.close()
        for src in datasets:
            src.close()

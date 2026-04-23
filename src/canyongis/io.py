from __future__ import annotations

import warnings
import numpy as np
import rasterio


def log(msg: str) -> None:
    print(f"[canyon] {msg}", flush=True)


def read_dem(path: str):
    """Return (elevation, transform, crs, pixel_size)."""
    with rasterio.open(path) as src:
        elev = src.read(1).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            elev[elev == nodata] = np.nan
        transform = src.transform
        crs = src.crs
        pixel_size = abs(transform.a)
    log(f"DEM loaded: shape={elev.shape}, pixel={pixel_size:.2f}m, crs={crs}")
    if not crs.is_projected:
        warnings.warn(
            "DEM is in a geographic CRS. Meter-based parameters will behave unpredictably. "
            "Reproject to UTM first: gdalwarp -t_srs EPSG:32612 in.tif out.tif"
        )
    return elev, transform, crs, pixel_size


def write_raster(path: str, arr: np.ndarray, transform, crs, dtype=None) -> None:
    if dtype is None:
        dtype = arr.dtype
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=dtype,
        crs=crs,
        transform=transform,
        compress="deflate",
        predictor=2,
        tiled=True,
    ) as dst:
        dst.write(arr.astype(dtype), 1)

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi


def meters_to_px(meters: float, pixel_size: float) -> int:
    return max(1, int(round(meters / pixel_size)))


def compute_slope(elev: np.ndarray, pixel_size: float) -> np.ndarray:
    """Slope in degrees via 3x3 Horn method (matches GDAL default)."""
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32) / (8 * pixel_size)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32) / (8 * pixel_size)
    filled = np.where(np.isnan(elev), np.nanmean(elev), elev)
    dzdx = ndi.convolve(filled, kx, mode="nearest")
    dzdy = ndi.convolve(filled, ky, mode="nearest")
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)
    slope_deg[np.isnan(elev)] = np.nan
    return slope_deg


def compute_tpi(elev: np.ndarray, pixel_size: float, radius_m: float) -> np.ndarray:
    """TPI = elevation minus circular focal mean."""
    r_px = meters_to_px(radius_m, pixel_size)
    y, x = np.ogrid[-r_px : r_px + 1, -r_px : r_px + 1]
    kernel = (x * x + y * y <= r_px * r_px).astype(np.float32)
    kernel /= kernel.sum()
    filled = np.where(np.isnan(elev), np.nanmean(elev), elev)
    focal_mean = ndi.convolve(filled, kernel, mode="nearest")
    return (elev - focal_mean).astype(np.float32)


def compute_local_relief(elev: np.ndarray, pixel_size: float, radius_m: float) -> np.ndarray:
    """Max minus min elevation in a circular window — proxy for cliff drop height."""
    r_px = meters_to_px(radius_m, pixel_size)
    y, x = np.ogrid[-r_px : r_px + 1, -r_px : r_px + 1]
    footprint = x * x + y * y <= r_px * r_px
    filled = np.where(np.isnan(elev), np.nanmean(elev), elev)
    relief = (
        ndi.maximum_filter(filled, footprint=footprint, mode="nearest")
        - ndi.minimum_filter(filled, footprint=footprint, mode="nearest")
    ).astype(np.float32)
    relief[np.isnan(elev)] = np.nan
    return relief

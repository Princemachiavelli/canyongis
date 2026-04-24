from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi


def meters_to_px(meters: float, pixel_size: float) -> int:
    return max(1, round(meters / pixel_size))


def fill_nans(elev: np.ndarray) -> np.ndarray:
    """Return elev with NaNs replaced by the non-NaN mean. Safe to share across callers."""
    return np.where(np.isnan(elev), np.nanmean(elev), elev).astype(np.float32, copy=False)


def compute_slope(
    elev: np.ndarray, pixel_size: float, *, filled: np.ndarray | None = None
) -> np.ndarray:
    """Slope in degrees via 3x3 Horn method (matches GDAL default)."""
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32) / (8 * pixel_size)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32) / (8 * pixel_size)
    if filled is None:
        filled = fill_nans(elev)
    dzdx = ndi.convolve(filled, kx, mode="nearest")
    dzdy = ndi.convolve(filled, ky, mode="nearest")
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)
    slope_deg[np.isnan(elev)] = np.nan
    return slope_deg


def compute_tpi(
    elev: np.ndarray,
    pixel_size: float,
    radius_m: float,
    *,
    filled: np.ndarray | None = None,
) -> np.ndarray:
    """TPI (Topographic Position Index) = elevation minus square focal mean. Uses a separable box filter (O(N))."""
    r_px = meters_to_px(radius_m, pixel_size)
    if filled is None:
        filled = fill_nans(elev)
    focal_mean = ndi.uniform_filter(filled, size=2 * r_px + 1, mode="nearest")
    return (elev - focal_mean).astype(np.float32)


def compute_local_relief(
    elev: np.ndarray,
    pixel_size: float,
    radius_m: float,
    *,
    filled: np.ndarray | None = None,
) -> np.ndarray:
    """Max minus min elevation in a square window — proxy for cliff drop height."""
    r_px = meters_to_px(radius_m, pixel_size)
    size = 2 * r_px + 1
    if filled is None:
        filled = fill_nans(elev)
    relief = (
        ndi.maximum_filter(filled, size=size, mode="nearest")
        - ndi.minimum_filter(filled, size=size, mode="nearest")
    ).astype(np.float32)
    relief[np.isnan(elev)] = np.nan
    return relief

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from ..hydrology import compute_flow_accum, compute_hand
from ..terrain import compute_local_relief, compute_slope, compute_tpi, fill_nans, meters_to_px
from .config import MLConfig


def build_feature_stack(
    elev: np.ndarray,
    transform,
    pixel_size: float,
    cfg: MLConfig,
) -> np.ndarray:
    """Compute the 6-channel input stack from a raw DEM.

    Channels (in order matching MLConfig.channel_names()):
      0. elev detrended (elev - uniform_filter(elev, detrend_radius_m))
      1. slope (degrees)
      2. TPI (tpi_radius_m)
      3. local relief (local_relief_radius_m)
      4. HAND clipped [0, hand_clip_m] then log1p
      5. log1p(flow_accum)

    Returns float32 array of shape (6, H, W). NaNs are replaced channel-wise.
    """
    filled = fill_nans(elev)
    nan_mask = np.isnan(elev)

    detrend_px = meters_to_px(cfg.detrend_radius_m, pixel_size)
    focal_mean = ndi.uniform_filter(filled, size=2 * detrend_px + 1, mode="nearest")
    elev_detrended = (filled - focal_mean).astype(np.float32)

    slope = compute_slope(elev, pixel_size, filled=filled)
    tpi = compute_tpi(elev, pixel_size, cfg.tpi_radius_m, filled=filled)
    local_relief = compute_local_relief(elev, pixel_size, cfg.local_relief_radius_m, filled=filled)

    flow_accum = compute_flow_accum(elev, transform)
    stream_mask = flow_accum >= cfg.flow_accum_stream_threshold
    if not stream_mask.any():
        # Fallback: use top-1% cells so HAND is defined everywhere.
        stream_mask = flow_accum >= np.nanpercentile(flow_accum, 99)
    hand = compute_hand(filled, stream_mask)

    hand_clipped = np.clip(hand, 0.0, cfg.hand_clip_m)
    hand_log = np.log1p(hand_clipped).astype(np.float32)
    flow_log = np.log1p(np.clip(flow_accum, 0.0, None)).astype(np.float32)

    # Replace NaNs in each channel with 0 (downstream normalization will handle scale).
    channels = [
        np.nan_to_num(elev_detrended, nan=0.0),
        np.nan_to_num(slope, nan=0.0),
        np.nan_to_num(tpi, nan=0.0),
        np.nan_to_num(local_relief, nan=0.0),
        hand_log,
        flow_log,
    ]
    stack = np.stack(channels, axis=0).astype(np.float32)

    # Propagate the NaN mask as an extra-invalid zero region (optional consumers may read this).
    stack[:, nan_mask] = 0.0
    return stack


def compute_norm_stats(stacks: list[np.ndarray]) -> dict[str, list[float]]:
    """Compute per-channel mean and std across a list of (6, H, W) stacks.

    Used at training time; the result is persisted as norm.json and consumed at inference.
    """
    if not stacks:
        raise ValueError("compute_norm_stats requires at least one stack")
    c = stacks[0].shape[0]
    sums = np.zeros(c, dtype=np.float64)
    sqsums = np.zeros(c, dtype=np.float64)
    counts = np.zeros(c, dtype=np.int64)
    for s in stacks:
        if s.shape[0] != c:
            raise ValueError(f"channel mismatch: expected {c}, got {s.shape[0]}")
        flat = s.reshape(c, -1)
        finite = np.isfinite(flat)
        sums += np.where(finite, flat, 0.0).sum(axis=1)
        sqsums += np.where(finite, flat * flat, 0.0).sum(axis=1)
        counts += finite.sum(axis=1)
    means = sums / np.maximum(counts, 1)
    variances = np.maximum(sqsums / np.maximum(counts, 1) - means**2, 0.0)
    stds = np.sqrt(variances)
    stds = np.where(stds < 1e-6, 1.0, stds)
    return {"mean": means.astype(np.float32).tolist(), "std": stds.astype(np.float32).tolist()}


def apply_norm(stack: np.ndarray, stats: dict[str, list[float]]) -> np.ndarray:
    """Z-score normalize a (C, H, W) stack with cached per-channel mean/std."""
    mean = np.asarray(stats["mean"], dtype=np.float32).reshape(-1, 1, 1)
    std = np.asarray(stats["std"], dtype=np.float32).reshape(-1, 1, 1)
    return ((stack - mean) / std).astype(np.float32)

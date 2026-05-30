from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage as ndi

from ..geomorphons import export_geomorphons
from ..terrain import compute_local_relief, compute_slope, compute_tpi, fill_nans, meters_to_px
from .config import MLConfig

GEOMORPHON_OUTPUTS = ("forms", "positive", "negative", "intensity")


def build_feature_stack(
    elev: np.ndarray,
    transform,
    pixel_size: float,
    cfg: MLConfig,
    dem_path: str | Path | None = None,
) -> np.ndarray:
    """Compute the DEM-derived input stack from a raw DEM.

    Channels (in order matching MLConfig.channel_names()):
      0. elev detrended (elev - uniform_filter(elev, detrend_radius_m))
      1. slope (degrees)
      2. TPI (tpi_radius_m)
      3. local relief (local_relief_radius_m)
      4. HAND clipped [0, hand_clip_m] then log1p
      5. log1p(flow_accum)
      6. log1p(distance_to_drainage_m)
      7+. optional geomorphon channels from precomputed GRASS r.geomorphon rasters

    Returns float32 array of shape (C, H, W). NaNs are replaced channel-wise.
    """
    filled = fill_nans(elev)
    nan_mask = np.isnan(elev)

    detrend_px = meters_to_px(cfg.detrend_radius_m, pixel_size)
    focal_mean = ndi.uniform_filter(filled, size=2 * detrend_px + 1, mode="nearest")
    elev_detrended = (filled - focal_mean).astype(np.float32)

    slope = compute_slope(elev, pixel_size, filled=filled)
    tpi = compute_tpi(elev, pixel_size, cfg.tpi_radius_m, filled=filled)
    local_relief = compute_local_relief(elev, pixel_size, cfg.local_relief_radius_m, filled=filled)

    from ..hydrology import compute_flow_accum, compute_hand

    flow_accum = compute_flow_accum(elev, transform, fill_depressions=cfg.flow_fill_depressions)
    stream_mask = flow_accum >= cfg.flow_accum_stream_threshold
    if not stream_mask.any():
        # Fallback: use top-1% cells so HAND is defined everywhere.
        stream_mask = flow_accum >= np.nanpercentile(flow_accum, 99)
    hand = compute_hand(filled, stream_mask)
    distance_to_drainage = ndi.distance_transform_edt(~stream_mask).astype(np.float32) * np.float32(
        pixel_size
    )

    hand_clipped = np.clip(hand, 0.0, cfg.hand_clip_m)
    hand_log = np.log1p(hand_clipped).astype(np.float32)
    flow_log = np.log1p(np.clip(flow_accum, 0.0, None)).astype(np.float32)
    distance_log = np.log1p(distance_to_drainage).astype(np.float32)

    # Replace NaNs in each channel with 0 (downstream normalization will handle scale).
    channels = [
        np.nan_to_num(elev_detrended, nan=0.0),
        np.nan_to_num(slope, nan=0.0),
        np.nan_to_num(tpi, nan=0.0),
        np.nan_to_num(local_relief, nan=0.0),
        hand_log,
        flow_log,
        distance_log,
    ]
    channels.extend(_build_geomorphon_channels(dem_path, elev.shape, transform, cfg))
    stack = np.stack(channels, axis=0).astype(np.float32)

    # Propagate the NaN mask as an extra-invalid zero region (optional consumers may read this).
    stack[:, nan_mask] = 0.0
    return stack


def geomorphon_sidecar_dir(dem_path: str | Path, search_m: float) -> Path:
    dem_path = Path(dem_path)
    return dem_path.with_suffix("").with_name(f"{dem_path.stem}.geomorphons") / f"{search_m:g}m"


def _ensure_geomorphon_outputs(dem_path: str | Path, search_m: float, cfg: MLConfig) -> Path:
    out_dir = geomorphon_sidecar_dir(dem_path, search_m)
    missing = [
        name for name in GEOMORPHON_OUTPUTS if not (out_dir / f"geomorphon_{name}.tif").exists()
    ]
    if not missing:
        return out_dir
    if not cfg.geomorphon_auto_generate:
        raise FileNotFoundError(
            f"missing geomorphon raster(s) for {dem_path} at {search_m:g}m: {', '.join(missing)}"
        )
    export_geomorphons(
        dem_path,
        out_dir,
        search_m=search_m,
        skip_m=cfg.geomorphon_skip_m,
        flat_deg=cfg.geomorphon_flat_deg,
        dist_m=cfg.geomorphon_dist_m,
        outputs=GEOMORPHON_OUTPUTS,
    )
    return out_dir


def _read_geomorphon_band(path: Path, shape: tuple[int, int], transform) -> np.ndarray:
    with rasterio.open(path) as src:
        if src.shape != shape:
            raise ValueError(
                f"geomorphon raster shape mismatch: {path} has {src.shape}, expected {shape}"
            )
        if not np.allclose(tuple(src.transform), tuple(transform), atol=1e-6):
            raise ValueError(f"geomorphon raster transform mismatch: {path}")
        arr = src.read(1, masked=True)
    data = np.asarray(np.ma.filled(arr.astype(np.float32), np.nan), dtype=np.float32)
    data[~np.isfinite(data)] = 0.0
    return data


def _build_geomorphon_channels(
    dem_path: str | Path | None,
    shape: tuple[int, int],
    transform,
    cfg: MLConfig,
) -> list[np.ndarray]:
    if not cfg.use_geomorphons:
        return []
    if dem_path is None:
        raise ValueError("dem_path is required when use_geomorphons=True")

    channels: list[np.ndarray] = []
    for search_m in cfg.geomorphon_search_m:
        out_dir = _ensure_geomorphon_outputs(dem_path, search_m, cfg)
        forms = _read_geomorphon_band(out_dir / "geomorphon_forms.tif", shape, transform)
        positive = _read_geomorphon_band(out_dir / "geomorphon_positive.tif", shape, transform)
        negative = _read_geomorphon_band(out_dir / "geomorphon_negative.tif", shape, transform)
        intensity = _read_geomorphon_band(out_dir / "geomorphon_intensity.tif", shape, transform)

        ridge_spur = np.isin(forms.astype(np.uint8), (3, 5)).astype(np.float32)
        hollow_valley = np.isin(forms.astype(np.uint8), (7, 9)).astype(np.float32)
        channels.extend(
            [
                ridge_spur,
                hollow_valley,
                positive.astype(np.float32, copy=False),
                negative.astype(np.float32, copy=False),
                intensity.astype(np.float32, copy=False),
            ]
        )
    return channels


def feature_stack_signature(cfg: MLConfig) -> dict[str, object]:
    return {
        "channel_names": cfg.channel_names(),
        "flow_fill_depressions": cfg.flow_fill_depressions,
        "use_geomorphons": cfg.use_geomorphons,
        "geomorphon_search_m": list(cfg.geomorphon_search_m),
        "geomorphon_skip_m": cfg.geomorphon_skip_m,
        "geomorphon_flat_deg": cfg.geomorphon_flat_deg,
        "geomorphon_dist_m": cfg.geomorphon_dist_m,
        # The cached weight raster is derived from these thresholds; changing them
        # must invalidate the cache so the weight is recomputed.
        "reliable_negative_relief_max_m": cfg.reliable_negative_relief_max_m,
        "reliable_negative_slope_max_deg": cfg.reliable_negative_slope_max_deg,
        "reliable_negative_distance_min_m": cfg.reliable_negative_distance_min_m,
        "ignore_weight": cfg.ignore_weight,
    }


def compute_norm_stats(stacks: list[np.ndarray]) -> dict[str, list[float]]:
    """Compute per-channel mean and std across a list of (C, H, W) stacks.

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

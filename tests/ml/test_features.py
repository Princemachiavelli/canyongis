from __future__ import annotations

import numpy as np
from rasterio.transform import from_origin

from canyongis.ml.config import MLConfig
from canyongis.ml.features import apply_norm, build_feature_stack, compute_norm_stats


def _synthetic_dem(h: int = 64, w: int = 64, pixel: float = 1.0) -> tuple[np.ndarray, object, float]:
    # Tilted plane + Gaussian valley so flow_accum has something to work with.
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    plane = 100.0 - 0.1 * yy - 0.05 * xx
    valley = -8.0 * np.exp(-0.5 * ((xx - w / 2) / 3.0) ** 2)
    elev = (plane + valley).astype(np.float32)
    transform = from_origin(0.0, h * pixel, pixel, pixel)
    return elev, transform, pixel


def test_feature_stack_shape_and_dtype():
    elev, transform, pixel = _synthetic_dem()
    cfg = MLConfig()
    stack = build_feature_stack(elev, transform, pixel, cfg)
    assert stack.shape == (cfg.in_channels, elev.shape[0], elev.shape[1])
    assert stack.dtype == np.float32


def test_feature_stack_no_nan_propagation():
    elev, transform, pixel = _synthetic_dem()
    elev[0, 0] = np.nan
    cfg = MLConfig()
    stack = build_feature_stack(elev, transform, pixel, cfg)
    # fill_nans + nan_to_num should leave no NaNs anywhere.
    assert not np.isnan(stack).any()


def test_norm_stats_and_apply_zero_mean_unit_std():
    rng = np.random.default_rng(0)
    cfg = MLConfig()
    stacks = [
        rng.normal(loc=5.0, scale=2.0, size=(cfg.in_channels, 32, 32)).astype(np.float32)
        for _ in range(4)
    ]
    stats = compute_norm_stats(stacks)
    assert len(stats["mean"]) == cfg.in_channels
    assert len(stats["std"]) == cfg.in_channels
    normed = apply_norm(stacks[0], stats)
    # Per-channel: mean should be close to 0; std close to 1 (roughly — single sample).
    means = normed.mean(axis=(1, 2))
    stds = normed.std(axis=(1, 2))
    assert np.all(np.abs(means) < 1.0)
    assert np.all(stds > 0.1)


def test_feature_stack_channel_names_match_config():
    cfg = MLConfig()
    assert len(cfg.channel_names()) == cfg.in_channels == 7

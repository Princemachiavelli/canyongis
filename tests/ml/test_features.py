from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from canyongis.ml.config import MLConfig
from canyongis.ml.features import (
    apply_norm,
    build_feature_stack,
    compute_norm_stats,
    geomorphon_sidecar_dir,
)


def _synthetic_dem(
    h: int = 64, w: int = 64, pixel: float = 1.0
) -> tuple[np.ndarray, object, float]:
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


def test_geomorphon_config_expands_channels():
    cfg = MLConfig(use_geomorphons=True, geomorphon_search_m=(10.0, 30.0))
    assert cfg.in_channels == 17
    assert len(cfg.channel_names()) == 17
    assert "geomorphon_10m_ridge_spur" in cfg.channel_names()
    assert "geomorphon_30m_intensity" in cfg.channel_names()


def test_feature_stack_appends_precomputed_geomorphon_channels(tmp_path):
    elev, transform, pixel = _synthetic_dem()
    dem_path = tmp_path / "dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        height=elev.shape[0],
        width=elev.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=transform,
    ) as dst:
        dst.write(elev, 1)

    out_dir = geomorphon_sidecar_dir(dem_path, 10.0)
    out_dir.mkdir(parents=True)
    forms = np.full(elev.shape, 6, dtype=np.uint8)
    forms[1, 1] = 3
    forms[2, 2] = 5
    forms[3, 3] = 7
    forms[4, 4] = 9
    forms[5, 5] = 255
    positive = np.ones(elev.shape, dtype=np.float32) * 2.0
    negative = np.ones(elev.shape, dtype=np.float32) * 3.0
    intensity = np.ones(elev.shape, dtype=np.float32) * -4.0
    for name, arr, dtype in [
        ("forms", forms, "uint8"),
        ("positive", positive, "float32"),
        ("negative", negative, "float32"),
        ("intensity", intensity, "float32"),
    ]:
        with rasterio.open(
            out_dir / f"geomorphon_{name}.tif",
            "w",
            driver="GTiff",
            height=elev.shape[0],
            width=elev.shape[1],
            count=1,
            dtype=dtype,
            crs="EPSG:26911",
            transform=transform,
            nodata=255 if name == "forms" else None,
        ) as dst:
            dst.write(arr, 1)

    cfg = MLConfig(
        use_geomorphons=True,
        geomorphon_search_m=(10.0,),
        geomorphon_auto_generate=False,
    )
    stack = build_feature_stack(elev, transform, pixel, cfg, dem_path=dem_path)
    assert stack.shape[0] == 12
    ridge_spur, hollow_valley, pos, neg, inten = stack[-5:]
    assert ridge_spur[1, 1] == 1.0
    assert ridge_spur[2, 2] == 1.0
    assert ridge_spur[3, 3] == 0.0
    assert ridge_spur[5, 5] == 0.0
    assert hollow_valley[3, 3] == 1.0
    assert hollow_valley[4, 4] == 1.0
    assert hollow_valley[5, 5] == 0.0
    assert np.all(pos == 2.0)
    assert np.all(neg == 3.0)
    assert np.all(inten == -4.0)

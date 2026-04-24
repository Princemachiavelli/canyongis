"""Smoke tests: import the package and exercise pure-numpy helpers on a tiny synthetic DEM."""

from __future__ import annotations

import numpy as np


def test_package_imports():
    import canyongis

    expected = {
        "MLConfig",
        "read_dem",
        "write_raster",
        "compute_slope",
        "compute_tpi",
        "compute_local_relief",
        "compute_flow_accum",
        "compute_hand",
        "build_feature_stack",
        "build_labels_from_kml",
        "run_training",
        "run_inference",
        "render_folium_map",
    }
    assert expected.issubset(set(canyongis.__all__))


def test_mlconfig_defaults_are_sane():
    from canyongis import MLConfig

    cfg = MLConfig()
    assert cfg.in_channels == 6
    assert len(cfg.channel_names()) == cfg.in_channels
    assert cfg.tile_size > 0
    assert 0.0 < cfg.label_sigma_m < 50.0


def test_compute_slope_flat_is_zero():
    from canyongis import compute_slope

    flat = np.zeros((32, 32), dtype=np.float32)
    slope = compute_slope(flat, pixel_size=1.0)
    assert slope.shape == flat.shape
    assert np.allclose(slope, 0.0)


def test_compute_slope_ramp_matches_arctan():
    """A constant-gradient ramp of dz/dx = 1 should yield arctan(1) = 45° everywhere interior."""
    from canyongis import compute_slope

    n = 32
    elev = np.tile(np.arange(n, dtype=np.float32), (n, 1))
    slope = compute_slope(elev, pixel_size=1.0)
    interior = slope[2:-2, 2:-2]
    assert np.allclose(interior, 45.0, atol=0.5)


def test_cli_help_exits_cleanly():
    import pytest

    from canyongis.__main__ import cli

    with pytest.raises(SystemExit) as excinfo:
        cli(["--help"])
    assert excinfo.value.code == 0

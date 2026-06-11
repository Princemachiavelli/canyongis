from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin

from canyongis.baseline import BaselineConfig, perona_malik, run_baseline
from canyongis.io import write_raster

X0, Y0 = 500_000.0, 4_000_000.0
CHANNEL_COL = 80
KNICK_ROW = 120


def _synthetic_canyon_dem(tmp_path):
    """South-sloping valley with a 15 m V-incision along x=80 and a 6 m step at row 120.

    The 0.3 m/px lateral tilt makes diagonal descent steeper than due south,
    so D8 flow converges into the incised channel.
    """
    h, w = 220, 160
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    elev = 300.0 - 0.2 * yy + 0.3 * np.abs(xx - CHANNEL_COL)
    elev -= np.clip(15.0 - 2.5 * np.abs(xx - CHANNEL_COL), 0.0, None)
    elev -= 6.0 * (yy > KNICK_ROW)
    transform = from_origin(X0, Y0, 1.0, 1.0)
    crs = rasterio.crs.CRS.from_epsg(32612)
    dem_path = tmp_path / "dem.tif"
    write_raster(dem_path, elev.astype(np.float32), transform, crs)
    return dem_path


def test_perona_malik_smooths_texture_preserves_step():
    rng = np.random.default_rng(0)
    z = np.zeros((64, 64), dtype=np.float32)
    z[:, 32:] = 10.0  # sharp edge
    noisy = z + rng.normal(0.0, 0.2, z.shape).astype(np.float32)
    smoothed = perona_malik(noisy, iters=30, dt=0.1)
    # Noise on the flats shrinks...
    flat = smoothed[:, 4:28]
    assert flat.std() < noisy[:, 4:28].std() * 0.7
    # ...while the 10 m step survives.
    assert smoothed[:, 40:].mean() - smoothed[:, :24].mean() > 9.0


def test_baseline_finds_canyon_and_knickpoint(tmp_path):
    dem_path = _synthetic_canyon_dem(tmp_path)
    out_dir = tmp_path / "out"
    cfg = BaselineConfig(
        pm_iters=30,
        min_area_m2=2000.0,
        depth_radius_m=40.0,
        confine_depth_m=4.0,
        min_length_m=40.0,
        min_drop_m=3.0,
    )
    result = run_baseline(dem_path, out_dir, cfg)

    assert result.n_centerlines >= 1
    lines = gpd.read_file(out_dir / "baseline_centerlines.gpkg")
    top = lines.sort_values("canyon_score", ascending=False).iloc[0]
    xs = np.asarray(top.geometry.coords)[:, 0]
    # Top-scored centerline tracks the incised channel column.
    assert abs(xs.mean() - (X0 + CHANNEL_COL + 0.5)) < 6.0
    assert top["length_m"] > 100.0
    assert top["mean_depth_m"] > 3.0
    assert top["net_drop_m"] > 10.0

    # The 6 m step shows up as a drop point near the knickpoint row.
    assert result.n_drops >= 1
    drops = gpd.read_file(out_dir / "baseline_drops.gpkg")
    ys = drops.geometry.y.to_numpy()
    assert ((ys < Y0 - (KNICK_ROW - 15)) & (ys > Y0 - (KNICK_ROW + 20))).any()
    assert (drops["drop_m"] >= 3.0).all()

    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["n_centerlines"] == result.n_centerlines
    for name in ("pm_curvature.tif", "depth.tif", "channel_mask.tif"):
        assert (out_dir / name).exists()


def test_baseline_empty_on_flat_dem(tmp_path):
    elev = np.full((96, 96), 100.0, dtype=np.float32)
    transform = from_origin(X0, Y0, 1.0, 1.0)
    crs = rasterio.crs.CRS.from_epsg(32612)
    dem_path = tmp_path / "flat.tif"
    write_raster(dem_path, elev, transform, crs)

    result = run_baseline(dem_path, tmp_path / "out", BaselineConfig(pm_iters=5))
    assert result.n_centerlines == 0
    assert result.n_drops == 0

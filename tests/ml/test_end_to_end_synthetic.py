"""End-to-end pipeline test on a tiny synthetic DEM + KML.

Exercises: prepare_cache → tile index → 1 epoch train → inference → vectorize.
Does NOT assert convergence; just that every stage runs and produces outputs
with correct shapes/types. Marked slow because it does a full forward+backward
pass through the SMP U-Net on CPU.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.ml.config import MLConfig

pytestmark = pytest.mark.slow


def _make_synthetic_dem(path: Path, h: int = 128, w: int = 128, pixel: float = 1.0):
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    plane = 100.0 - 0.1 * yy - 0.05 * xx
    # Carve a vertical valley at column w/2.
    valley = -8.0 * np.exp(-0.5 * ((xx - w / 2) / 2.0) ** 2)
    elev = (plane + valley).astype(np.float32)
    transform = from_origin(500000.0, 4100000.0 + h * pixel, pixel, pixel)
    crs = rasterio.crs.CRS.from_epsg(26911)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
        crs=crs, transform=transform,
    ) as dst:
        dst.write(elev, 1)
    return transform, crs


def _make_synthetic_kml(path: Path, transform, crs, h: int = 128, w: int = 128, pixel: float = 1.0) -> None:
    """Trace the synthetic valley with a polyline in WGS84 KML."""
    # Column w/2 in pixel space → UTM x = origin_x + (w/2) * pixel
    origin_x = transform.c
    origin_y = transform.f
    col = w / 2
    # Offset by 1 px to simulate label imprecision.
    line_utm = [(origin_x + (col + 1) * pixel, origin_y - r * pixel) for r in range(2, h - 2)]
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    line_wgs = [t.transform(x, y) for x, y in line_utm]
    coords = " ".join(f"{x},{y},0" for x, y in line_wgs)
    path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Full Descent</name><LineString><coordinates>{coords}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )


def test_e2e_train_then_predict(tmp_path: Path):
    # --- setup ---
    dem_path = tmp_path / "dem.tif"
    kml_path = tmp_path / "trace.kml"
    transform, crs = _make_synthetic_dem(dem_path, 128, 128, 1.0)
    _make_synthetic_kml(kml_path, transform, crs, 128, 128, 1.0)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([{"kml_path": str(kml_path), "dem_path": str(dem_path)}]))

    out_dir = tmp_path / "out"

    # Tiny config for speed.
    cfg = MLConfig(
        tile_size=64,
        train_stride=32,
        predict_overlap=16,
        super_tile_size=64,
        n_folds=2,
        epochs=1,
        warmup_epochs=0,
        freeze_encoder_epochs=0,
        early_stop_patience=99,
        grad_accum_steps=1,
        batch_size_cpu=2,
        cache_workers=1,
        num_workers=0,
        encoder_name="resnet18",
        encoder_weights=None,  # skip ImageNet download
        manifest_path=str(manifest_path),
        out_dir=str(out_dir),
        render_folium=False,
    )

    # --- train ---
    from canyongis.ml.train import run_training

    summary = run_training(cfg)
    assert "mean_cldice" in summary
    # Checkpoint exists for at least fold 0.
    ckpt = out_dir / "fold0" / "best.pt"
    assert ckpt.exists(), "fold0 checkpoint not written"

    # --- predict ---
    predict_out = tmp_path / "predict"
    pcfg = MLConfig(
        dem_path=str(dem_path),
        checkpoint_path=str(ckpt),
        out_dir=str(predict_out),
        tile_size=64,
        predict_overlap=16,
        encoder_weights=None,
    )

    from canyongis.ml.predict import run_inference

    stats = run_inference(pcfg)
    assert "centerlines" in stats
    assert (predict_out / "canyon_prob.tif").exists()
    assert (predict_out / "rappel_prob.tif").exists()

    with rasterio.open(predict_out / "canyon_prob.tif") as src:
        prob = src.read(1)
    assert prob.shape == (128, 128)
    assert prob.dtype == np.float32
    # Values should be in [0, 1].
    assert prob.min() >= 0.0 and prob.max() <= 1.0

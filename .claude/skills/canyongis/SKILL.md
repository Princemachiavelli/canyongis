---
name: canyongis
description: How to use the canyongis CLI and Python modules — Ropewiki KML/DEM ingest, DEM mosaicking, U-Net training, and tiled inference. Use when running, debugging, or extending any canyongis pipeline step.
---

# canyongis usage skill

End-to-end pipeline: **fetch Ropewiki KML + USGS 1m DEM → mosaic per route → train U-Net → tiled inference**. All steps have a console script and an underlying Python entry point.

Always run inside `nix develop`. Do not `pip install` — the user owns Nix flake dependency additions. Do not background ML runs (memory-heavy; user watches for OOM live).

---

## 1. Fetch Ropewiki KML + DEM

**CLI:** `canyongis-ropewiki-sync`
**Python:** `canyongis.ingest.ingest_ropewiki_cli` → `ingest_ropewiki(url, cache_dir, ...)`

Pulls a route page from Ropewiki via the MediaWiki API, extracts the descent KML, then queries The National Map (TNM) for matching USGS 1m DEM tiles.

```bash
canyongis-ropewiki-sync "https://ropewiki.com/Willow_Creek_Canyon" \
  --cache-dir data/ropewiki
```

Outputs:
- `data/ropewiki/kml/<route>.kml`
- `data/ropewiki/dem/<tile>.tif` (one per intersecting USGS 1m quad)
- A per-route manifest entry (route name, KML path, list of DEM tiles, bbox).

Useful when scripting many routes: call `ingest_ropewiki()` directly and pass a list of URLs.

---

## 2. Mosaic DEM tiles per route

**CLI:** `canyongis-mosaic-manifest`
**Python:** `canyongis.ml.manifest_mosaic.build_mosaic_manifest`

For each KML in the input manifest:
1. Buffer the descent track by `--buffer-m` (default 2000 m).
2. Mosaic intersecting DEM tiles, reproject to the route's UTM zone, fill nodata.
3. Validate: ≥ 20 % of descent pixels must land on valid DEM, else the route is dropped.
4. Write a new manifest pointing each route at its single mosaic GeoTIFF.

```bash
canyongis-mosaic-manifest data/manifest.json data/manifest-mosaic.json \
  --dem-dir data/ropewiki/dem \
  --kml-dir data/ropewiki/kml \
  --output-dem-dir data/ropewiki/dem/mosaic \
  --buffer-m 2000 \
  --download-missing
```

`--download-missing` triggers TNM fetches for any tile a KML touches that isn't already cached.

---

## 3. Train

**CLI:** `canyongis-ml-train`
**Python:** `canyongis.ml.cli.train_cli` → `canyongis.ml.train.run_training(config, manifest, outdir)`
**Config:** `canyongis.ml.config.MLConfig` (every field is a CLI flag with the same name).

Spatial K-fold CV; per fold writes `best.pt`, `last.pt`, metrics, and a cached `features.tif` per route under `outdir/`.

```bash
canyongis-ml-train data/manifest-mosaic.json runs/001/train \
  --epochs 60 \
  --encoder-name resnet18 \
  --lr 1e-4 \
  --batch-size-cuda 6 --batch-size-mps 4 \
  --tile-size 256 \
  --positive-tile-ratio 0.8 \
  --warmup-epochs 5 \
  --freeze-encoder-epochs 20
```

Flags worth knowing:
- `--device {auto,cuda,mps,cpu}` — `auto` picks via `ml/device.py:get_device()`
- `--encoder-name` — any SMP encoder; default `resnet18` (fits 8 GB VRAM)
- `--folds N`, `--fold-only K` — run a single fold
- `--canyon-loss-weight`, `--rappel-loss-weight`, `--cldice-iter`, `--focal-gamma` — loss tuning
- Feature radii: `--tpi-radius-m 200`, `--local-relief-radius-m 20`, `--use-geomorphons`

The dataset is Zarr-backed (`ml/dataset.py`); features are computed once per route and reused across folds/epochs.

---

## 4. Inference (tiled)

**CLI:** `canyongis-ml-predict`
**Python:** `canyongis.ml.cli.predict_cli` → `canyongis.ml.predict.run_inference(dem_path, ckpt, outdir, ...)`

Sliding-window prediction with Hann × Gaussian blending, then post-process: extract canyon centerlines, rappel candidates, and per-rappel drop distances.

```bash
canyongis-ml-predict dem.tif runs/001/train/best.pt runs/001/pred \
  --tile-size 256 \
  --predict-overlap 64 \
  --predict-batch-size 4 \
  --device auto
```

Outputs in `runs/001/pred/`:
- `canyon_prob.tif`, `rappel_prob.tif` — heatmaps
- `canyon_centerline.geojson`, `rappels.geojson`
- `features.tif` (cached input stack — visualize with `canyongis-ml-export-features`)
- `preview.html` (Folium) unless `--no-folium`

The default `canyongis <dem> <outdir>` (no subcommand) runs the **classical** pipeline (no ML) — slope/HAND/flow-accum heuristics + Folium render. Use `canyongis-ml-predict` for the trained model.

---

## 5. Inspection helpers

```bash
# Render the cached multiband feature stack as PNGs
canyongis-ml-export-features runs/001/train/features.tif out/pngs/

# r.geomorphons (GRASS) raster + colored previews
canyongis-geomorphons dem.tif out/ --search-m 30
canyongis-geomorphon-previews out/

# Drop-distance rasters and route overlays
canyongis-drop-previews dem.tif out/ --distances "10,25,50,100"

# KML sanity check
canyongis-validate route.kml
```

---

## Python entry points (for scripting / notebooks)

```python
from canyongis.ingest import ingest_ropewiki
from canyongis.ml.manifest_mosaic import build_mosaic_manifest
from canyongis.ml.config import MLConfig
from canyongis.ml.train import run_training
from canyongis.ml.predict import run_inference
from canyongis.ml.device import get_device, batch_size_for, amp_enabled

cfg = MLConfig(epochs=60, encoder_name="resnet18", lr=1e-4)
run_training(cfg, manifest="data/manifest-mosaic.json", outdir="runs/001/train")

run_inference("dem.tif", "runs/001/train/best.pt", "runs/001/pred",
              tile_size=256, predict_overlap=64)
```

---

## Troubleshooting

- **OOM on CUDA (8 GB):** lower `--batch-size-cuda`, keep `resnet18`, drop `--tile-size` to 192.
- **MPS dtype/AMP errors:** AMP is CUDA-only by design (`ml/device.py:amp_enabled()`). Don't force it on Mac.
- **Mosaic step drops a route:** the KML descent buffer has < 20 % valid DEM coverage. Re-run with `--download-missing` or check the KML CRS.
- **`ImportError` for a new dep:** ask the user to add it to `flake.nix` / `nix/package.nix`. Don't `pip install`.
- **Slow tests:** they're real training runs — `pytest -m "not slow"` for fast iteration.

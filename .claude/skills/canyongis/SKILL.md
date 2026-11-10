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
- `--canyon-loss-weight`, `--rappel-loss-weight`, `--cldice-iter`, `--focal-gamma`, `--focal-beta` — loss tuning
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

## 4b. Reach ranker / evaluator (primary detector; see `DETECTION_STRATEGY.md`)

Ranks D8 drainage reaches (A ≥ 1 ha, ~50 m long) instead of segmenting pixels. Features
are cached per mosaic/tile, so re-scoring after a label or model change takes seconds.

```bash
canyongis-reach-eval /data/canyongis/training-manifest-grouped.json runs/reach-eval-NNN \
  --root /data/canyongis \           # manifest paths are relative to it
  --ropewiki \                       # unmapped Ropewiki canyons → positive/ignore; non-technical → ignore
  --max-pixels 60e6 --tile-px 4000 \ # tile big mosaics (catchment area from a 4x-decimated full read)
  --extra-train-manifest /data/canyongis/training-manifest-new24-grouped.json \  # train-only
  --exclude-track "Das Pfeife Canyon" \  # short, no marked rappels: ignore, don't label
  --raster unet=runs/predict-x/canyon_prob.tif   # score any probability raster ({stem} allowed)
```

Run it from the repo (not from `/data/canyongis`: the dev shell's hook creates a Postgres
dir in the cwd) and pass `--root`.

- Outputs: `report.md`, `metrics.json`, `reaches/<stem>.gpkg` (layers `reaches`, `steps`,
  `iso_steps`), `png/<stem>.png` (TP green / FP red / FN blue / ignore gray), and
  `review/fp_runs.{gpkg,csv}`: main-stem runs of negative reaches above the threshold,
  longest × highest-scoring first, with the nearest Ropewiki canyon.
- **Smoothing:** `--smooth-hops 1 --smooth median` (default) smooths learned scores along
  the main stem only (never across a junction). Comma lists score several variants; the
  first drives outputs and the threshold.
- **Profile steps:** `--profile` (default) adds isotonic-profile step features (`iso_*`,
  cached as `<stem>.<sig>.prof-<sig>.npz`) and scores them as rappel candidates. They size
  and count rappels better than D8 drops but don't help ranking, so they're not ranker
  features unless passed via `--features`. Tune with `--step-grade/--step-window-m/
  --min-step-m/--corridor-px`.
- **Labels:** `--label-snap-m 40` map-matches tracks onto the network instead of the 20 m
  pixel tolerance (per-track offsets in the report; opt-in, ranking-neutral).
  `--label-overrides FILE` applies reviewed labels (vector file, `label` =
  positive/negative/ignore, optional `buffer_m`). Overriding reviewed FPs raises AP by
  construction: report results with and without.
- **More data:** `--extra-train-manifest` mosaics are trained on but never scored, so
  results stay comparable on the same held-out terrain.
- CV is leave-one-group-out (overlapping mosaics/tiles share a group).
- Labels default to `--technical markers`: positives are the descent stretch spanning named
  rappel points (R1, rap 2, … via `KmlFeatures.anchor_points()`) ± 200 m, and the rest of the
  descent is ignored. `--unmarked ignore` drops tracks without usable markers; `--technical off`
  uses whole descent lines.
- AP depends on how rare positives are, so compare it with the base rate. AUC doesn't.
- Always read the `*_am` (area-matched) columns: they score only reaches at least as large as
  the 10th-percentile positive, so "it's the biggest drainage" can't win. Compare `ap_am`
  with `base_rate_am`.
- Feature cache key = `ReachConfig.signature()` (label settings excluded). Bump
  `reaches.CACHE_VERSION` when feature code changes.
- `canyongis-drainage-diag <manifest> out.json` checks track ↔ D8 alignment per mosaic.

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

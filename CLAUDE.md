# canyongis

Geospatial + ML pipeline for detecting canyoneering descents (canyon centerlines, rappel anchors, drops) from 1m USGS DEMs, trained on Ropewiki KML descent tracks.

## Repo layout

- `src/canyongis/` — Python package
  - `__main__.py` — top-level `canyongis` CLI dispatcher (`train` / `predict` / default Folium pipeline)
  - `config.py`, `io.py`, `terrain.py`, `hydrology.py`, `masks.py`, `vectors.py`, `mapping.py` — classical DEM analysis (slope, TPI, local relief, HAND, flow accumulation, channel/cliff/rappel masks, Folium render)
  - `ingest.py` — Ropewiki MediaWiki API + TNM 1m DEM downloader
  - `validate.py` — KML descent-track sanity checks
  - `geomorphons.py`, `geomorphon_preview.py`, `drop_preview.py` — GRASS r.geomorphon export + preview rasters
  - `ml/` — U-Net training & tiled inference
    - `cli.py` — `train_cli`, `predict_cli`
    - `config.py` — `MLConfig` dataclass (all tunables exposed as CLI flags)
    - `device.py` — `get_device()` (cuda > mps > cpu), `amp_enabled()` (cuda only), `batch_size_for()`
    - `model.py` — `CanyonUNet` (SMP U-Net, default `resnet18`, two 1×1-conv heads: canyon + rappel)
    - `features.py` — feature stack (elev_detrended, slope, TPI, local_relief, HAND, flow_accum, dist_to_drainage, ±geomorphons)
    - `dataset.py` — Zarr-backed tile sampler (`positive_tile_ratio=0.8`)
    - `augment.py`, `labels.py`, `losses.py` (BCE+clDICE+focal+PU)
    - `train.py` — spatial K-fold CV, warmup, encoder freeze, early stop
    - `predict.py` — tiled sliding window with Hann×Gaussian blending; extracts centerlines, rappels, drop distances
    - `manifest_mosaic.py` — buffer KMLs by 2km, mosaic matching DEM tiles, validate ≥20% valid descent coverage
    - `inspect.py` — features.tif → PNG previews
- `tests/` — pytest smoke tests; slow end-to-end tests marked `@pytest.mark.slow`
- `nix/` — `package.nix`, `pysheds.nix` (NumPy 2.0 patch), `treefmt.nix`
- `flake.nix` — flake-parts; Python 3.13; CUDA 8.6 on `x86_64-linux`, MPS on `aarch64-darwin`

## Console scripts

Defined in `pyproject.toml [project.scripts]`:

| Script | Entry | What it does |
|---|---|---|
| `canyongis` | `__main__:cli` | Dispatch (`train`/`predict`) or default predict + Folium render |
| `canyongis-validate` | `validate:cli` | Validate KML descent tracks |
| `canyongis-ropewiki-sync` | `ingest:ingest_ropewiki_cli` | Fetch Ropewiki KML + matching TNM 1m DEM tiles |
| `canyongis-mosaic-manifest` | `ml.manifest_mosaic:mosaic_manifest_cli` | Build buffered DEM mosaics, update training manifest |
| `canyongis-ml-train` | `ml.cli:train_cli` | Train U-Net with K-fold CV |
| `canyongis-ml-predict` | `ml.cli:predict_cli` | Tiled inference from checkpoint |
| `canyongis-ml-export-features` | `ml.inspect:export_feature_pngs_cli` | features.tif → PNGs |
| `canyongis-geomorphons` | `geomorphons:geomorphons_cli` | r.geomorphon raster export |
| `canyongis-geomorphon-previews` | `geomorphon_preview:geomorphon_previews_cli` | Colored geomorphon PNGs |
| `canyongis-drop-previews` | `drop_preview:drop_previews_cli` | Drop-distance rasters + route overlays |

## Dev environment

- **Setup:** `nix develop` (do not `pip install` — Python deps are added to the Nix flake by the user)
- **Format/lint:** `nix fmt` (nixfmt + ruff format + ruff check --fix + taplo)
- **Test:** `pytest`; slow tests via `pytest -m slow`
- **CI gate:** `nix flake check`
- **Ruff:** 100-char lines, py311+ target, rules `E,W,F,I,B,UP,SIM,RUF`

## Hardware notes

- 8 GB VRAM CUDA target **and** macOS MPS must both work.
- Default encoder: `resnet18` (small enough for 8 GB).
- AMP is CUDA-only (`amp_enabled()` returns False on MPS/CPU; no `GradScaler` off CUDA).
- Default batch sizes: cuda=6, mps=4, cpu=2.
- ML pipeline runs are memory-heavy — the user runs them manually to catch OOM live; **don't background them.**

## Canonical workflow

```bash
# 1. Pull KML + DEM tiles for a route
canyongis-ropewiki-sync "https://ropewiki.com/Willow_Creek_Canyon" --cache-dir runs/001/cache

# 2. Mosaic DEMs per KML (buffered) and update manifest
canyongis-mosaic-manifest data/manifest.json data/manifest-mosaic.json \
  --dem-dir data/ropewiki/dem --kml-dir data/ropewiki/kml \
  --output-dem-dir data/ropewiki/dem/mosaic --buffer-m 2000 --download-missing

# 3. Train (K-fold CV)
canyongis-ml-train data/manifest-mosaic.json runs/001/train \
  --epochs 60 --encoder-name resnet18 --lr 1e-4

# 4. Predict on a fresh DEM
canyongis-ml-predict dem.tif runs/001/train/best.pt runs/001/pred \
  --tile-size 256 --predict-overlap 64
```

For a detailed how-to (flags, Python entry points, troubleshooting), see `.claude/skills/canyongis/SKILL.md`.

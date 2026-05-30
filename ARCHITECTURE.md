# ARCHITECTURE.md

## Overview

`canyongis` detects canyon/drainage features in high-resolution Digital Elevation Models (DEMs) using a supervised deep learning model trained on KML annotations. A multi-task U-Net consumes a 7-channel feature stack derived from the DEM and produces two outputs: a canyon probability raster and a rappel/cliff-drop keypoint heatmap. Vector centerlines and rappel points are extracted from the rasters. Outputs include GeoTIFF rasters, GeoPackage/KML vectors, and an optional interactive Folium HTML map.

The earlier rule-based detector (`masks.py`, hand-tuned thresholds on slope, TPI, HAND, flow accumulation) has been replaced. The terrain and hydrology modules remain as feature extractors for the model.

## Pipeline Stages

1. **Input & Preprocessing**
   - Read DEM (single-band, projected CRS).
   - Validate resolution and CRS; warn on geographic CRS.
   - Fill NaNs and depressions so flow routing produces a continuous network.

2. **Feature Stack Construction** (`ml/features.py`)
   Seven channels per pixel, all z-score normalized with per-channel statistics cached at training time:
   1. Elevation detrended (`elev − uniform_filter(elev, 500 m)`)
   2. Slope (Horn method, degrees)
   3. TPI (Topographic Position Index, 200 m radius)
   4. Local relief (max−min in a 20 m window)
   5. HAND (clipped to [0, 100 m], `log1p`)
   6. `log1p(flow_accum)` (D8 via pysheds)
   7. `log1p(distance_to_drainage_m)` from the thresholded flow-accumulation stream mask

3. **Label Generation** (`ml/labels.py`)
   - Parse KML files with `validate.parse_kml` (reuses the existing classifier that separates descent lines, approaches, and rappel points by name).
   - Rasterize descent polylines to a 1 px-wide mask, then apply a distance-transform Gaussian (`y = exp(-0.5 * (d / σ)²)`) to produce soft labels that tolerate label offset error.
   - Rasterize rappel/drop-in points and blur into a keypoint heatmap.
   - `σ` is configurable per KML via the manifest (higher for less precise traces).
   - **Reliable-negative weight raster.** A buffered mosaic almost always contains canyons that are *not* in the KML, so its unlabeled interior cannot be treated as negative space. `build_reliable_negative_weight` emits a per-pixel trust weight in [0, 1]: `1` for the labeled positive corridor and for "reliable negatives" (gentle, low-relief terrain far from any drainage — thresholds `reliable_negative_{relief_max_m,slope_max_deg,distance_min_m}`); `ignore_weight` (default `0`) everywhere else, dropping unknown pixels from the loss. Cached as `weight.tif` alongside the labels and carried through augmentation as a synced mask.

4. **Model** (`ml/model.py`)
   - U-Net backbone via `segmentation_models_pytorch` with a **ResNet-18 encoder** (default; fits 8 GB VRAM at batch 6–8 × 256² with AMP, and runs on macOS MPS at batch 4 fp32). ResNet-34 is a config switch for when data scales to thousands of KMLs.
   - Two 1×1-conv heads on top of the decoder: `head_canyon` (segmentation) and `head_rappel` (keypoint heatmap).

5. **Loss & Training** (`ml/losses.py`, `ml/train.py`)
   - **Canyon loss** = `0.5 · soft-BCE-PU + 0.5 · soft-clDice`, both masked by the reliable-negative weight raster (per-pixel, in [0, 1]). Soft-BCE-PU down-weights the negative term by `(1 − π)` so unlabeled pixels aren't treated as strong negatives (positive-unlabeled learning framing); the weight further zeroes out unknown pixels entirely. Soft-clDice (Shit et al. 2021) preserves thin-curvilinear topology — its **precision term is masked by the weight** so the model is *not* penalized for correctly lighting up unlabeled canyons (recall is driven by the target skeleton, which lives only in the labeled corridor, so it is unaffected).
   - **Rappel loss** = Gaussian focal loss (CornerNet/CenterNet variant; α=2, β=4) on the keypoint heatmap. Standard focal loss assumes hard binary targets and its modulator collapses on Gaussian-soft labels; the CornerNet form separates the focal exponent on the prediction from a `(1−y)^β` heatmap-aware down-weighting on the negative term.
   - AdamW, cosine LR schedule with warmup, AMP on CUDA only (MPS autocast is unreliable as of torch 2.2).
   - **Spatial K-fold cross-validation** (default K=5). Whole DEMs/canyons are assigned to seeded, balanced folds so no tile from a held-out DEM appears in training.
   - Positive-biased tile sampling via `WeightedRandomSampler` (80% of draws come from tiles with label mass).
   - Encoder frozen for the first 20 epochs to stabilize the heads on tiny data.

6. **Inference & Vectorization** (`ml/predict.py`)
   - Sliding 256×256 window with 64 px overlap.
   - Per-window prediction is accumulated into a full-raster output with a **2D Hann × Gaussian blending window** (`σ = tile/6`), dividing by the summed weight at the end to suppress tile seams.
   - Outputs: `canyon_prob.tif`, `rappel_prob.tif`.
   - Centerlines: threshold `canyon_prob ≥ 0.5` → `skimage.morphology.skeletonize` → custom pixel-path tracer that splits at branch points → shapely LineStrings → GPKG + KML.
   - Rappel points: `skimage.feature.peak_local_max` on the heatmap with `min_distance = 4 m / pixel_size`.

7. **Visualization** (`mapping.py`)
   - Folium HTML map with Esri Topo + Satellite base layers, centerlines as GeoJSON, and rappel points as color-coded circle markers sorted by probability.

## Module Layout

```
src/canyongis/
  __main__.py          # top-level CLI; chains run_inference + folium render
  io.py                # read_dem, write_raster, write_vector, log
  terrain.py           # compute_slope, compute_tpi, compute_local_relief, fill_nans
  hydrology.py         # compute_flow_accum (pysheds), compute_hand (EDT)
  mapping.py           # render_folium_map (ML output schema)
  validate.py          # KML parser + post-hoc detection scoring
  vectors.py           # score_routes (generic descent/grade/cliff-count scorer)
  config.py            # thin shim re-exporting MLConfig as Config for back-compat
  ml/
    config.py          # MLConfig dataclass — all ML tunables, auto-exposed via argparse
    device.py          # get_device() → cuda > mps > cpu; amp_enabled()
    features.py        # build_feature_stack; compute_norm_stats; apply_norm
    labels.py          # build_labels_from_kml_paths (polylines → soft label + rappel heatmap)
    dataset.py         # ManifestEntry, prepare_cache, build_tile_index,
                       # SpatialKFoldSplitter, TileDataset, make_weighted_sampler
    augment.py         # albumentations pipeline tuned for DEM channels
    model.py           # CanyonUNet (SMP U-Net + two heads)
    losses.py          # soft_bce_pu, soft_dice, soft_cldice, gaussian_focal_loss, MultiTaskLoss
    train.py           # run_training, train_fold — K-fold driver
    predict.py         # run_inference, tiled sliding-window, skeleton → polylines
    cli.py             # canyongis-ml-train, canyongis-ml-predict entry points
```

## Manifest Format

Training consumes a JSON manifest of `{kml_paths, dem_path, label_sigma_m?, rappel_sigma_m?, precision_tier?}` entries. Older single-track `{kml_path, dem_path}` rows are still accepted:

```json
[
  {"kml_paths": ["/data/kml/foo.kml", "/data/kml/bar.kml"], "dem_path": "/data/dem/foo-bar.tif", "label_sigma_m": 5.0, "precision_tier": "high"},
  {"kml_paths": ["/data/kml/baz.kml"], "dem_path": "/data/dem/baz.tif", "label_sigma_m": 15.0, "precision_tier": "low"}
]
```

Per-entry `label_sigma_m` widens the Gaussian falloff for less precise KMLs, absorbing their offset error without downweighting them out of the training set.

## CLIs

```
canyongis-ml-train MANIFEST OUTDIR [--tile-size 256] [--epochs 60] ...
canyongis-ml-predict DEM CHECKPOINT OUTDIR
canyongis DEM CHECKPOINT OUTDIR [--no-folium]           # inference + Folium render
canyongis train  ...                                     # sub-delegates to ml-train
canyongis predict ...                                    # sub-delegates to ml-predict
canyongis-validate --kml GROUND_TRUTH.kml --out-dir OUT  # post-hoc scoring
```

## Key Dependencies

- **PyTorch ≥ 2.2** with CUDA 12.x (Linux) or MPS (macOS) — provided by the Nix flake.
- **segmentation-models-pytorch** — U-Net + pretrained encoders.
- **albumentations** — multi-target augmentation synced across image + label + keypoint masks.
- **scikit-image** — skeletonization and peak detection.
- **rasterio, geopandas, shapely, pyproj, pyogrio, folium** — geospatial I/O and rendering.
- **pysheds** — D8 flow accumulation; provided via Nix, not pip.

## Extensibility

- Swap encoders by setting `MLConfig.encoder_name` (any encoder SMP supports — `resnet34`, `efficientnet-b0`, etc.).
- Add feature channels: extend `features.build_feature_stack` and update `MLConfig.channel_names()` and `in_channels`.
- Alternate loss weights via `MLConfig.canyon_loss_weight`, `rappel_loss_weight`, `bce_cldice_ratio`.
- Parallel cache preparation via `MLConfig.cache_workers` (default 2), separate from training DataLoader workers.
- Spatial split seed via `MLConfig.spatial_seed` for reproducibility experiments.

# Feature & ML Pipeline Review

Review of the DEM feature stack and U-Net training pipeline against prior art in
channel/gully extraction (GeoNet, gully-erosion DL) and lidar-archaeology DL (RVT/VAT).
Findings ordered by expected impact.

---

## 1. Likely root causes of poor results

### 1.1 `flow_accum_stream_threshold = 200` is ~100× too low (config.py)

200 cells on a 1 m DEM = **200 m² of drainage area**. Channel initiation in the
literature is 10⁴–10⁶ m²; GeoNet-style workflows use hectares. Consequences cascade:

- The stream mask is hyper-dense (a "stream" every few tens of meters), so **HAND is
  near zero everywhere** and carries little contrast between canyon bottoms and benches.
- `distance_to_drainage` is small everywhere, so the reliable-negative condition
  `distance > 50 m` is almost never satisfied → **the reliable-negative set is nearly
  empty** (see 1.2).

**Fix:** express the threshold in m² and default to ~5×10⁴ m² (5 ha) at 1 m
(`50_000` cells). Sanity-check per mosaic: the stream mask should trace obvious
drainages, not carpet the tile. Consider a second HAND channel at a larger threshold
(e.g., 5×10⁵ m²) for main-stem context.

### 1.2 The PU loss sees almost no negatives

`build_reliable_negative_weight` requires `local_relief(20 m) < 2 m` AND
`slope < 10°` AND `distance_to_drainage > 50 m`. On 1 m DEMs of canyon country,
relief < 2 m over a 41×41 m window is rare even on mesa tops, and 1.1 kills the
distance term. With `ignore_weight = 0`, the canyon loss reduces to
**positives + a sliver of trivially-flat negatives**. The decision boundary is
unconstrained on every pixel that looks remotely like terrain — steep slopes, ridges,
ordinary valleys — which is the classic PU failure mode: the model lights up all
incised/steep terrain and the 0.5 threshold produces blobs.

**Fixes (combine):**
- Loosen thresholds (relief < 5 m, slope < 15°) **after** fixing 1.1, and verify the
  reliable-negative fraction per mosaic (target maybe 20–60% of pixels; log it).
- Add geomorphically-implausible classes as reliable negatives: geomorphon
  ridge/spur/peak, or simply `TPI > +k` — a canyon bottom cannot be a ridge.
  This gives the model *hard* negatives (steep convex terrain), not just flat plains.
- Set `ignore_weight` to a small nonzero value (0.05–0.15) so unknown regions exert
  weak negative pressure instead of none, in the spirit of the `(1-pi)` PU weighting.
- Note: `pu_prior = 0.01` makes `soft_bce_pu` multiply the negative term by 0.99 —
  i.e., the PU correction is cosmetic. The trust mask does all the work. Either drop
  the pretense or implement nnPU (Kiryo et al. 2017) with a positive-risk correction.

### 1.3 Label corridors vs. GPS error in canyons

`label_sigma_m = 5`, `label_floor = 0.05` → the positive corridor dies ~12 m from the
track. GPS multipath in deep/slot canyons routinely produces **10–50 m horizontal
error** (sliver-of-sky geometry; urban-canyon RMSE ≥13 m is documented). So the
labeled corridor frequently sits on a canyon wall or bench while the true bottom is
weight-0 "unknown" — the model is *trained* on mislocated positives.

**Fixes:**
- **Snap descent tracks to the drainage line before rasterizing**: within a search
  radius (~50 m), move each vertex to the nearest high-flow-accumulation / minimum-HAND
  cell (standard stream-snapping). This is probably the single highest-leverage label
  improvement and also makes `label_sigma_m = 5` defensible. Keep the original track as
  a fallback when no drainage exists within the radius (e.g., slickrock approaches).
- Until then, widen `label_sigma_m` to 10–15 m (the per-entry `label_sigma_m` /
  `precision_tier` plumbing already exists — use it).
- Verify `validate.parse_kml` truly excludes approach/exit hike segments from
  `descent_lines`; an approach trail labeled positive poisons the canyon head.

### 1.4 The rappel head cannot learn from its loss

`gaussian_focal_loss` is **mean-reduced over all pixels** with no weight mask. Rappel
targets (σ = 3 m points) occupy ~100 of 65 536 pixels on the rare tile that has any;
most tiles are all-zero. CornerNet/CenterNet normalize by the **number of peaks**, not
the pixel count — with mean reduction the gradient from positives is diluted ~10⁴× and
the head converges to "always 0". Also, rappel anchors are sparsely/unreliably mapped
in Ropewiki KMLs (a PU problem with no mask at all here).

**Fixes:** divide the summed focal loss by `max(num_positive_pixels, 1)` per batch
(CenterNet convention); restrict the negative term to pixels near the labeled descent
corridor (rappels can't exist away from the route — everything else is unknown);
longer-term, derive extra supervision from geometry (elevation drop along the snapped
track) rather than KML points alone.

---

## 2. Feature stack improvements

### 2.1 You already compute the best features — but only for previews

`rvt_preview.py` produces **negative openness, SVF, positive openness, local
dominance, MSRM, MSTP** — and none of them enter `build_feature_stack`. The
gully/channel-detection and lidar-archaeology literature consistently ranks
**negative openness** (terrain concavity/incision) and **SVF** at the top for incised
features; MSTP is explicitly designed as a 3-channel multi-scale CNN input.

**Recommended additions (priority order):**
1. `opns_neg` — negative openness (the canonical "incision" signal).
2. `svf` — sky-view factor (canyon bottoms see little sky; complements slope).
3. Plan/profile **curvature** on a smoothed DEM — the core GeoNet discriminant
   (Perona-Malik filter + positive geometric curvature ⇒ channelized pixel). A
   Gaussian σ≈2–4 m pre-smooth is an acceptable stand-in for Perona-Malik.
4. Multi-scale TPI — one scale (200 m) misses both slots (10–30 m wide) and broad
   canyons; add ~50 m and ~500 m variants (cheap box filters).

`rvt-py` is already a dependency, so 1–2 are nearly free. Cache invalidation via
`feature_stack_signature` already handles the channel change.

### 2.2 Parameter notes on existing features

- `tpi_radius_m = 200` — keep, but as the middle of a multi-scale set (50/200/500).
- `local_relief_radius_m = 20` — good for rappel-scale cliffs; a 100 m variant would
  capture canyon depth rather than single drops.
- `hand_clip_m = 100` + log1p — fine once the stream threshold (1.1) is fixed.
- `detrend_radius_m = 500` — fine.
- Geomorphons default **off**; when on, single 30 m search. Multi-scale
  (30/100/300 m) geomorphons are well-supported in the literature and you already
  support multiple scales — consider enabling by default once GRASS availability in
  the Nix env is settled, since 1.2 wants the ridge/spur classes.

### 2.3 Normalization & pretraining mismatch

- Global per-channel z-score across canyons is OK, but `elev_detrended`/relief have
  heavy-tailed, region-dependent distributions; robust scaling (median/IQR) is safer.
- **ImageNet encoder + 7+ physical channels is a weak combination.** SMP re-initializes
  (or tiles) `conv1` for ≠3 channels, then `freeze_encoder_epochs = 20` freezes an
  encoder whose first conv has never seen terrain data for a third of training.
  Two coherent paths:
  a. Keep the physical multi-channel stack → train encoder **from scratch**
     (drop `encoder_weights`, drop the freeze, maybe a smaller encoder), or
  b. Keep ImageNet weights → feed 3-channel RVT composites (VAT or MSTP), which is
     exactly how the archaeology DL results (ADAF, Maya-area detectors) get transfer
     to work.
  Current config does neither.

---

## 3. Training/eval mechanics

- **Augmentation bug:** `A.GaussNoise(std_range=(0.3, 0.3))` runs **after** z-score
  normalization and on **all channels**, and in albumentations `std_range` for float
  images is a fraction of 1.0 — so this injects σ=0.3 noise *in z-units* into slope,
  HAND, flow-accum, etc., not "0.3 m of elevation noise" as `elev_noise_std_m`
  implies. That is enormous, correlated-feature-destroying noise on 30% of tiles.
  Apply noise to the elevation-derived channel(s) only, in feature units, before
  normalization — or drop it.
- **Model selection metric:** `val_cldice` is computed on weight-masked soft labels;
  tiles with empty target skeleton get `t_rec = 1` and the score is dominated by the
  smooth terms. Add a metric that means something operationally: buffer-tolerant
  precision/recall of the extracted centerline vs. the (snapped) track at 10/25/50 m
  tolerances, computed per-fold on stitched full-DEM predictions, not per-tile.
- **Wasted negative batches:** with `positive_tile_ratio = 0.8`, the 20% negative
  tiles in steep terrain have near-all-zero weight → zero gradient. After fixing 1.2
  this improves; also consider indexing tiles by weight mass and sampling negatives
  only where the weight mask is non-trivial.
- **Calibration:** with masked/PU training, sigmoid outputs are not calibrated;
  `prob_threshold = 0.5` is arbitrary. Sweep the threshold against the buffer-PR
  metric per fold and store the best in the checkpoint.
- `in_channels` default (7) silently re-derives in `__post_init__`; fine, but
  checkpoint↔config drift on geomorphon settings will only fail at load time —
  consider validating channel names at predict time (norm.json already implies count).

## 4. Strategic: build the analytical baseline first

With (likely) tens of labeled canyons, a 14 M-param U-Net is data-starved, and the
classical pipeline is strong prior art: **GeoNet** (Perona-Malik smoothing →
curvature threshold → cost-minimizing geodesic centerlines) extracts channel networks
from 1 m lidar without any training. A canyon scorer that walks the flow-accumulation
network and scores segments by cross-sectional confinement (HAND/relief at ±50 m),
channel slope, and drop count would:
- give an honest baseline the U-Net must beat,
- generate **dense pseudo-labels / candidate negatives** (scored drainages elsewhere
  in the mosaic) to fix the PU problem at its source, and
- already produce the rappel/drop analytics (drops along profile) deterministically.

---

## Sources

- [GeoNet: automatic extraction of channel heads/networks from high-resolution topography (Passalacqua et al.)](https://www.researchgate.net/publication/303469652_GeoNet_An_open_source_software_for_the_automatic_and_objective_extraction_of_channel_heads_channel_network_and_channel_morphology_from_high_resolution_topography_data)
- [Objective extraction of channel heads from high-resolution topographic data (Clubb et al., WRR 2014)](https://agupubs.onlinelibrary.wiley.com/doi/full/10.1002/2013WR015167)
- [Automated marine clay gully delineation using deep learning (ESP 2024)](https://onlinelibrary.wiley.com/doi/10.1002/esp.5833)
- [Gully detection based on LiDAR and terrain derivatives with deep learning](https://loreabad6.github.io/Gullies/)
- [Objective comparison of relief visualization techniques with deep CNN for archaeology (JAS Reports)](https://www.sciencedirect.com/science/article/pii/S2352409X2100239X)
- [ADAF: automatic detection of archaeological features from ALS using deep learning (Čož, Kokalj et al.)](https://www.sciencedirect.com/science/article/pii/S2352409X26001689)
- [VAT method as an alternative to hillshaded DEM (Remote Sensing 2019)](https://www.mdpi.com/2072-4292/11/24/2946)
- [GPS accuracy: HDOP/PDOP/multipath (GIS Geography)](https://gisgeography.com/gps-accuracy-hdop-pdop-gdop-multipath/)
- [GPS reception in canyons (GPS Tracklog)](https://gpstracklog.com/2005/11/using_gps_in_ca.html)
- [Satellite positioning accuracy in urban canyons (PMC, 13 m horizontal RMSE)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12349109/)
- clDice (Shit et al. 2021); CornerNet/CenterNet focal heatmap loss (Law & Deng 2018; Zhou et al. 2019); nnPU (Kiryo et al. 2017).

# Canyon / Rappel Detection Strategy Review (2026-10-29)

Why the current models fail, what the data actually says, and a fast-iteration
approach to replace per-pixel U-Net segmentation as the primary detector.

---

## 1. Current techniques (inventory)

| Approach | Where | Status |
|---|---|---|
| U-Net segmentation (canyon soft-label + rappel heatmap), PU mask, clDICE | `ml/` | Trained; poor FP/FN |
| GeoNet baseline: Perona-Malik → Laplacian heads → D8 trace → depth score → profile drops | `baseline.py` | New, not yet evaluated |
| Multi-scale TPI / geomorphon / plan-curvature score → hysteresis → skeleton → trunk extraction | `ravine_compare.py`, `ravine_spine.py` | Visual comparison only |
| Track snapping to a raster (min/max) | `kml_snap.py` | Not wired into ML labels |
| Drop-to-lower-ground, band-pass drops, track profile rappel detection | `drop_preview.py`, `track_profile.py` | Previews / per-track only |
| RVT (openness, SVF, VAT, MSTP) | `rvt_preview.py` | Previews only — not model inputs |

None of these share an evaluator, so none can be compared or tuned against ground truth.

## 2. Why the U-Net results are bad

1. **No meaningful metric → no real iteration.** `train-geomorphons-grouped01` reports
   `mean_cldice = 0.845` while maps are full of FP/FN; best-epoch `val_dice` per fold is
   0.02–0.38.
   The weight-masked tile clDICE is dominated by smoothing terms and empty-skeleton
   tiles. Every tuning decision so far has been made against a number that doesn't
   track quality.
2. **The fixes in `ML_FEATURE_REVIEW.md` were never applied or tested.** The most recent
   recorded runs (`train-geomorphons-grouped01`, predictions) date from May 2–3, i.e.
   *before* `e841a28` added the reliable-negative mask — so that work has never been
   evaluated either. The failed run's
   `training_config.json`: `flow_accum_stream_threshold = 200` (200 m² — streams
   everywhere, HAND/distance ≈ 0), ImageNet encoder on 12 physical channels,
   `label_sigma_m = 5` with unsnapped tracks, mean-reduced rappel focal loss.
3. **`pu_prior = 0.9`** in that run. `soft_bce_pu` (same formula before and after
   `e841a28`, and with no trust mask at the time) multiplies *every* negative term by
   `1 − π = 0.1`, so negatives count 10× less than positives — a direct recipe for
   lighting up all incised terrain (the FP pattern you're seeing).
4. **Rappel head is unlearnable from this data.** Only **34 rappel placemarks across 17
   of 100 KMLs**. `train_loss_rappel` collapses to ~0.002 by epoch 6 — it learned
   "always zero".
5. **Problem framing is too hard for ~100 tracks.** Pixel segmentation forces the net
   to rediscover *where drainages are* (a solved hydrology problem) *and* decide *which
   ones are canyons*, from a single 1-pixel line per canyon.

## 3. What the data says (diagnostic on 16 grouped mosaics, 1 m, 500 m buffer)

Script: D8 accumulation via `hydrology.compute_flow_dir_accum`, descent tracks
rasterized via `ml.labels._rasterize_lines`. Medians across mosaics (min–max):

| Measure | Result |
|---|---|
| Track → nearest D8 stream (A ≥ 1 ha), median distance | **5 m** (1–12) |
| Fraction of track within 15 m of a stream, A ≥ 1 ha | **0.91** (0.61–0.99) |
| Fraction of track within 15 m of a stream, A ≥ 5 ha | 0.88 (0.57–0.98) |
| Stream px near a route / all stream px, A ≥ 1 ha | 0.08 (0.04–0.17) |
| Stream px near a route / all stream px, A ≥ 5 ha | 0.16 (0.08–0.33) |

Single-feature separability (AUC, route-adjacent stream px vs other stream px, A ≥ 1 ha):

| Feature | Median AUC (min–max) |
|---|---|
| depth (−TPI) 150 m | **0.81** (0.58–0.89) |
| log drainage area | 0.80 (0.57–0.90) |
| depth 400 m | 0.79 (0.44–0.95) |
| depth 50 m | 0.78 (0.59–0.93) |
| mean slope, 31 px window | 0.66 (0.30–0.74) |
| relief within 25 m | 0.59 (0.31–0.72) |
| pixel slope | 0.47 (0.20–0.57) |

**Takeaways**
- **Candidate generation is solved.** ~90% of every known route lies on the D8 network
  at A ≥ 1 ha. The detector should never search off-network.
- **The real task is ranking drainage reaches.** Only 1 in 6–12 network pixels is
  on a route; with buffered mosaics that is a small, tractable ranking problem.
- **Cheap features already separate well** (AUC ≈ 0.8 single-feature), but two biases
  pull in opposite directions: (a) some "negative" reaches are real unlabeled canyons
  (AUC understated); (b) each mosaic is a 500 m buffer centred on its route, so the
  route is usually the trunk drainage and most negatives are short side gullies on its
  walls (AUC overstated — especially `log_area` — and the 1-in-6–12 base rate is far
  too optimistic). On a fresh DEM, big open washes will top an area ranking, so area
  must never be used alone; honest FP rates need Alimony or wider buffers.
- Raw slope is anti-correlated (likely because the negatives are steep wall gullies);
  confinement and catchment size carry the signal; drops must be measured along the
  channel profile, not as a disk-relief.
- Outliers: Lytle Creek South Fork's track sits a median 12–13 m off the network
  (likely a systematic hand-drawn offset → snap it); Trail Canyon/Prospect drains small
  catchments, so it drops out at A ≥ 20 ha (don't set the area threshold high).

## 4. Proposed approach: drainage-reach ranking

> This is the original proposal; §5–6 describe what was built. Differences: the tool is
> `canyongis-reach-eval`, labels use a 20 m track tolerance (no `kml_snap` snapping yet),
> scikit-learn is now in the flake, and the Ropewiki rappel properties
> (`Has number of rappels`, `Has longest rappel`) were verified and are in use.

### Step 0 — Evaluator first (this is the iteration loop)

`canyongis-eval` (new): for a mosaic set, with leave-one-mosaic-out splits:
1. Build the D8 network at A ≥ 1 ha; split into **reaches** (~50 m, also broken at
   junctions). Cache reach geometry + pixel indices per mosaic.
2. Label reaches from tracks **snapped to the network** (`kml_snap.py`, ≤ 30 m):
   positive if ≥ 50% of the reach is within 15 m of a snapped descent line;
   **ignore** if within 100 m of a route but not positive, or near a known Ropewiki
   canyon with no KML (see caveats); negative otherwise.
3. Score any method that yields a per-reach score (sample a raster along the reach,
   or match vector centerlines with a 15 m buffer):
   - reach-level **average precision**, **recall at precision 0.5**
   - route-length recall at 15/30 m buffer; FP length per km² outside ignore zones
   - per-mosaic HTML/PNG: TP/FP/FN reaches over hillshade
4. Score the existing `runs/predict-*` U-Net outputs, `canyongis-baseline`, and the
   trivial rankers (area alone, depth150 alone) to set the current bar.

Target runtime: features cached once (minutes); every scoring/tuning run in seconds.

### Step 1 — Per-reach features + simple model

Features (all on raw or lightly smoothed DEM, sampled per reach):
- **Catchment:** log drainage area at the reach outlet.
- **Confinement:** −TPI at 50/150/400 m; cross-section wall height left/right at
  ±25/50/100 m perpendicular to flow, **min-of-sides** (a canyon needs two walls);
  valley width at +5 m / +20 m above the thalweg; height/width ratio.
- **Profile:** channel gradient over the reach and ±200 m; **count and max size of
  steps ≥ 3 m over ≤ 10 m run** (from `baseline._detect_drops`) — the defining
  canyoneering trait; knickpoint intensity (2nd difference of smoothed profile).
- **Context:** the same features averaged upstream/downstream along the network
  (canyons are continuous); negative openness / SVF at the thalweg (rvt-py is
  already a dependency).

Model: logistic regression first (interpretable coefficients, ~seconds), then
gradient-boosted trees. **`scikit-learn` and/or `lightgbm` must be added to the
flake** (not present in the dev shell today); a torch/numpy logistic regression works
until then.

Post-processing: smooth scores along the network (e.g. 1-D median/HMM over
consecutive reaches) so outputs are contiguous canyon runs, then emit
centerlines in the same format as `canyongis-ml-predict`.

### Step 2 — Rappels: deterministic, on ranked reaches only

Drop the learned rappel head. On reaches scoring above threshold, detect drops from
the raw-DEM thalweg profile (window/min-drop tuned by grid search in the evaluator).
Validate on the 34 KML rappel points as a spot check, and — if Ropewiki's SMW exposes
them (verify property names) — against per-canyon rappel count / longest rappel.
Note: 1 m DEM thalwegs under-resolve slots; expect counts to be biased low.

### Step 3 — Return to the U-Net only after the ranker sets the bar

Options once Step 1 has a number to beat:
- Train on **network-centered patches** as a classifier (is this reach a canyon?)
  rather than full-tile segmentation.
- Use ranker scores as dense pseudo-labels / hard negatives to fix the PU problem.
- If continuing pixel segmentation: apply `ML_FEATURE_REVIEW.md` §1 fixes,
  `pu_prior ≈ 0.01–0.05`, from-scratch encoder (or 3-channel RVT input with ImageNet),
  and select checkpoints with the Step 0 evaluator — never tile clDICE.

## 5. Implementation status (2026-10-29)

Steps 0–2 are implemented:

- `src/canyongis/reaches.py`: D8 reach network, per-reach features, labels, tiling
  with catchment area from a 4×-decimated full-mosaic read, cached `.npz`.
- `src/canyongis/reach_eval.py` (`canyongis-reach-eval`): label refinement from
  Ropewiki, leave-one-group-out logreg + gradient boosting (scikit-learn), area-matched
  metrics, external-raster scoring, rappel-step checks, GPKG/PNG outputs.
- `src/canyongis/drainage_diag.py` (`canyongis-drainage-diag`): the §3 diagnostic.
- `ingest.py`: `USER_AGENT = "RopeGeo/1.0"` (Cloudflare challenged the old agent);
  `query_ropewiki_canyon_props` (technical rating, rappel count/longest, KML URL).

## 6. Results: `/data/canyongis/runs/reach-eval-001`

All 18 grouped mosaics: the 16 small ones plus Alimony (+67) and Allison (+6) tiled
at 4000 px. That's 51 inputs in 18 CV groups. 196 km of positive reaches against
3,572 km of negatives (base rate ≈ 5%). Ropewiki added 34 unlabeled technical
canyons as positives; 65 unlabeled canyons without a KML became 300 m ignore zones;
9 non-technical routes were ignored.

| method (leave-one-group-out) | AUC | AP | AP area-matched (base 0.097) | R@P50 |
|---|---|---|---|---|
| logreg | 0.888 | 0.345 | 0.388 | 0.275 |
| gradient boosting | 0.886 | 0.379 | 0.411 | 0.261 |
| logreg without `log_area` | 0.884 | 0.328 | 0.374 | 0.216 |
| best single feature (`depth150`) | 0.857 | 0.287 | 0.336 | 0.141 |
| `log_area` alone | 0.817 | 0.174 | 0.185 | 0.000 |
| U-Net `predict-fold0-test` (8 inputs) | 0.754 | 0.287 | 0.338 (base 0.162) | 0.024 |
| logreg on the same reaches | 0.840 | 0.383 | 0.417 (base 0.162) | 0.288 |

What this shows:
- **Ranker ≫ area.** With the large mosaics included, area alone falls to AP 0.17,
  and dropping `log_area` barely hurts the ranker. Confinement (−TPI 150 m,
  cross-section walls, narrow valley width) carries the signal. The early "ranker
  ≈ area" result was an artifact of the small route-centered mosaics.
- **The ranker beats the U-Net on the same reaches** (AP 0.38 vs 0.29, R@P50 0.29
  vs 0.02). This is weak evidence: it's one May checkpoint, and it may have trained
  on these areas.
- **Absolute performance is still modest.** At precision 0.5 it finds about a quarter
  of route length. Per-tile AP ranges from 0.02 to 0.9. The worst tiles (e.g. Alimony
  `r3250_c3250`) have long, deep main-stem canyons scored as false positives. Some of
  these may be real but unmapped descents.
- **Profile-step features are weak** (AUC ≈ 0.43–0.68). Along a 1 m D8 thalweg,
  drops don't separate routes from steep gullies well. The logreg coefficients on
  `ctx_max_drop` (+15) and `ctx_max_step_excess` (−11) are a collinear pair, so they
  aren't interpretable on their own.

Rappels (`runs/reach-eval-003-base`: 104 canyons, 175 named rappel points; steps on
positive reaches):

| step excess ≥ | rappel point within 25 m | steps / track km | Spearman vs Ropewiki rappel count |
|---|---|---|---|
| 0 m | 0.86 | 15.6 | 0.45 |
| 4 m | 0.71 | 3.8 | 0.62 |
| 8 m | 0.40 | 1.4 | 0.55 |

DEM profile steps **do localize rappels**: at excess ≥ 4 m, 71% of named rappels have a
step within 25 m, at about 4 candidates per km. An earlier version of this table showed
26%, but that came from a regex bug. `validate.RAPPEL_POINT_PATTERN` didn't match the
"R1", "R2 30 ft" names most Ropewiki KMLs use, and it counted drop-ins as rappels. The
new `ANCHOR_POINT_PATTERN` / `KmlFeatures.anchor_points()` fix this, and
`RAPPEL_POINT_PATTERN` now also matches R#/rap#. **That widens the U-Net's rappel-head
labels too:** earlier U-Net runs trained on drop-ins plus a few "rappel"-named points.

### Label-noise experiments (`runs/reach-eval-002-*`)

Mosaic names like `Alimony_Creek_plus_67` and `Allison_Mine_Loop_plus_6` come from the
grouping step (first KML + count). They are regions holding 68 and 7 routes, not single
canyons. Alimony Creek itself contributes only 0.38 km of track.

The longest positive track is Iron Fork (North and South): 17.6 km and **0 rappels** on
Ropewiki, about 9% of all positive length. Two options test whether long non-technical
tracks hurt:

| variant | logreg AP | area-matched AP / base rate | gbt AP |
|---|---|---|---|
| default (`reach-eval-001`) | 0.345 | 0.388 / 0.097 (4.0×) | 0.379 |
| `--min-rappels 1` (drops Iron Fork, Little Santa Anita Upper) | 0.303 | 0.342 / 0.084 (4.1×) | 0.340 |
| `--balance-canyons` (equal training weight per canyon) | 0.337 | 0.382 / 0.097 (3.9×) | 0.358 |
| both | 0.313 | 0.357 / 0.084 (4.3×) | 0.329 |

Neither option changes ranking quality much. Raw AP isn't comparable when the label set
changes, so compare lift over the base rate. Both are kept as flags, default off.

### Technical sections only (`runs/reach-eval-003-*`, `runs/reach-eval-005-*`)

`--technical markers` (now the default) labels only the descent stretch from 200 m before
the first named rappel to 200 m after the last. The rest of the descent is ignored, neither
positive nor negative. A track counts as marked with ≥ 2 rappel points, or 1 when Ropewiki
lists a single rappel. When a KML has rappel points but no line named as a descent, its
unclassified lines are used (never approach/exit ones). Parts join only at shared
endpoints: an earlier version split self-crossing GPS tracks into fragments and produced
sections of a few meters.

**Only 32 of 119 positive tracks are marked. On those, the technical section (± 200 m)
is 34.6 of 61.1 descent km (57%).** Most of each descent line is walking.

| labels | positive km | logreg AUC | logreg AP (base rate) | area-matched AP (base) |
|---|---|---|---|---|
| whole descent lines (`003-base`) | 196 | 0.888 | 0.345 (≈0.05) | 0.388 (0.097), 4.0× |
| sections ± 200 m; unmarked tracks whole (`005-tech200-whole`, default) | 175 | 0.905 | 0.349 (≈0.047) | 0.394 (0.094), 4.2× |
| sections ± 200 m only; unmarked ignored (`005-tech200-ignore`) | 39 | **0.916** | 0.135 (≈0.011) | 0.168 (0.023), **7.3×** |

Cleaner positives rank better: AUC rises, and so does lift over the base rate. AP falls in
the strict variant only because positives become much rarer. The cost is that just 32
canyons remain to train on.

### More marked KMLs from Ropewiki (`canyongis-ropewiki-survey`)

`runs/ropewiki-survey-socal`: in a Southern California bbox there are 311 technical
(rating 3/4) canyons on Ropewiki, and 216 have a usable KML. **54 of those mark their
rappels (58.7 technical km). 24 aren't in the manifest; 2 lie inside existing mosaics
(Eaton, Toypurina) and 22 need DEM tiles.** Their page URLs are in
`marked_not_in_manifest.txt`, ready for `canyongis-ropewiki-sync`. Some are desert slots
or in Nevada (Palm Wash, Syncline Slot, Keyhole, Rainbow), which is different terrain
from the San Gabriels.

## 7. Next iterations (each a cached re-run of `canyongis-reach-eval`)

1. **Look at the false positives** in `png/` and the `reaches/*.gpkg` files, especially
   Alimony `r0_c7250`, `r3250_c3250`, `r3250_c7250` and `r7250_c11250`. Decide which
   are unmapped canyons (send them to ignore or positive) and which are true errors.
2. **Better drop features:** take the profile along a smoothed or lowest-path thalweg
   instead of raw D8, measure drop over 3–5 m windows, and count steps per reach
   *relative to the neighboring reaches*.
3. **Add a feature group that's known to work:** negative openness / SVF at the
   thalweg (rvt-py is already a dependency), and plan curvature from `baseline.py`.
4. **Network smoothing of scores** (median or HMM along the main stem) so outputs are
   contiguous canyon runs. Score the result with the same evaluator.
5. **More technical-section labels from Ropewiki:** sync the 22 marked canyons that need
   DEMs (`marked_not_in_manifest.txt`), and run the survey on other regions (wider bbox)
   for more KMLs that already mark their rappels.
6. **Rappel tuning:** grid-search window/min-drop against the 175 named rappels, and
   try a minimum-elevation corridor (± 3 m) instead of the single D8 pixel path.
7. **Speed:** Alimony tiles take ~3–4 min each to build (pure-Python link walking +
   cross sections). Fine once cached; numba would help if the network/feature code
   changes often.

### Status of the items above (2026-11-09, §9)

Item 1 has tooling but still needs your review. Item 2 is done; it sizes rappels better but doesn't help ranking. Item 4 is done, with a small gain. Item 5 is partly done: 13 of 24 canyons added. Items 3, 6 and 7 are still open.

## 8. Caveats / open items

- **Unlabeled canyons:** now partly handled through Ropewiki (§6). Canyons that
  aren't on Ropewiki still count as negatives.
- **Catchment area** in small mosaics is still truncated at the 500 m buffer.
  Tiles use a decimated full-mosaic area. A regional flow-accumulation product (e.g.
  NHDPlus HR or a 10 m 3DEP run) would fix both.
- All data is SoCal (San Gabriels / Lytle Creek). Thresholds tuned here may not
  transfer to Colorado Plateau slickrock, where routes cross bare rock and slots are
  narrower than 1 m DEM resolution.

## 9. Iteration 2026-11-09: smoothing, profile steps, snapped labels, more data

All runs are under `/data/canyongis/runs/reach-eval-006-*` and share `reach-eval-006-cache`, which was seeded from 001. The settings match §6: `--ropewiki --max-pixels 60e6 --tile-px 4000`, technical markers, and unmarked tracks kept whole. Logreg is leave-one-group-out over all 51 inputs (18 groups). The ladder scores feature and smoothing changes on the **same labels**, so AP is comparable across those rows. Label changes are checked by cross-training instead.

| step | run | logreg AUC | AP | AP_am (base 0.094) | R@P50 |
|---|---|---|---|---|---|
| baseline (reproduces §6) | `006-base` | 0.905 | 0.349 | 0.394 | 0.262 |
| + isotonic step features in the ranker | `006-profile` | 0.906 | 0.350 | 0.394 | 0.260 |
| main-stem smoothing ±1, median (new default) | `006-default` | 0.904 | 0.351 | 0.397 | 0.272 |
| + 13 new canyons, train-only | `006-newdata` | 0.905 | 0.355 | 0.398 | 0.280 |
| gbt, same run | `006-newdata` | 0.898 | **0.372** | **0.403** | 0.227 |
| final: + exclude Das Pfeife (label change: positives 175.31 → 175.24 km) | `006-final` | 0.905 | 0.354 | 0.398 | 0.270 |
| gbt, same run (smoothed) | `006-final` | 0.898 | 0.376 | 0.408 | 0.237 |

- **Smoothing:** ±1 reach helps a little, mostly R@P50 (+0.01–0.02). ±2, 3 and 5 are worse, and mean is no better than median. Smoothing only follows main-stem edges (`main_up[down[i]] == i`), so a side gully doesn't inherit the score of the canyon it joins.
- **Isotonic profile steps** (`ProfileConfig`): elevation is the 3×3 corridor minimum along the D8 path, fit with a non-increasing least-squares profile per link. A step is a contiguous run steeper than 0.7 over 4 m with a total height of at least 3 m.
  - **Ranking:** single-feature AUC is only 0.48–0.58, and adding them doesn't change the ranker. Steep wall gullies have drops too.
  - **Rappels:** they're much better at sizing. Largest step vs Ropewiki's longest rappel gives ρ 0.56 (D8 drops: 0.32). Step count (≥ 8 m) vs Ropewiki rappel count gives ρ 0.68 (D8: 0.62).
  - **Locating rappels:** no better. At matched density, about 3.7 candidates/km, they find 64% of named rappels within 25 m, vs 71% for D8 drops at excess ≥ 4 m.
  - So they're opt-in ranker features (`--features`), but always reported as rappel candidates and written to the `iso_steps` layer.
- **Snapped labels** (`--label-snap-m 40`, `006-snap40`): tracks are map-matched onto the network, staying on the current link where possible, with gaps filled along `down`. Positive length drops from 175 to 154 km, because parallel reaches within 20 m no longer count.
  - Median per-track offset is 4.9 m (max 23.7 m). Lytle South Fork matches 100% at a 12.3 m offset, which the 20 m tolerance already handled.
  - Cross-training shows it's neutral. Training on snapped labels scores AP 0.342 on the old labels (vs 0.350 when trained on them) and 0.347 on the snapped labels (vs 0.342). It stays opt-in.
  - Its per-track stats are a useful audit. The worst match was Das Pfeife: 38% of points within 40 m. It's a very short canyon with no marked rappels, so it's now excluded (`--exclude-track`).
- **More data:** the 13 new canyons from `marked_not_in_manifest.txt` live in `training-manifest-new24-grouped.json`. The others were dropped: 3 have no KML, 5 have no descent line, Rue de Fortuna hit a TNM query error, Laurel Springs had no covering DEM, and Antenna didn't appear in the sync log at all.
  - With the new canyons as training-only data, scored on the same 18 groups, logreg gains a little and gbt gains more (AP 0.353 → 0.372).
  - Held-out scores on the new mosaics themselves are high for Keyhole (NV, AUC 0.99) and Palm Wash (0.98). They're poor for Toypurina (0.48), and for Cadillac Crash and Titanium Chalupa (≈ 0.75). Toypurina's DEM is complete (100% valid under the track), so the cause is unexplained; check its outcome PNG.
- **Review loop:** `review/fp_runs.{gpkg,csv}` lists negative main-stem runs above the threshold, sorted by length × mean score, with the nearest Ropewiki canyon. Mark them in an overrides file (`label` = positive/ignore/negative) and pass `--label-overrides`. Overriding false positives raises AP by construction, so report results both with and without.

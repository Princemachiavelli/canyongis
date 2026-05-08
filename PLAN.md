# Label-free canyon extraction plan

## Why pivot away from the U-Net

Ropewiki KMLs are descent tracks, not full canyon centerlines. Most DEM tiles
contain unlabeled drainages that are valid canyons. Any supervised loss treats
those as negatives, so the model learns to predict only the labeled stubs and
fragments along otherwise-correct drainages. Increasing PU prior helps but does
not fully fix the underlying label sparsity.

A purely geometric pipeline:

- Needs no labels at inference or training time.
- Surfaces the *full* canyon-form network on any DEM, not just the labeled subset.
- Has tunable, interpretable thresholds.
- Can use the Ropewiki KMLs as a *validation* signal (recall on known descents)
  rather than as supervision.

## What we're actually extracting

**Not** the hydrological drainage (D8 flow accumulation). Drainage volume is a
poor proxy for canyon-likeness — a high-watershed trunk can be flat, a small
headwater can be a slot. We want the **topographic ravine network**: the
connected set of hollows/valleys bounded by ridges and spurs, defined by *form*,
not *flow*.

Working definition: a canyon segment is a ravine axis where the local relief
(ridge → ravine drop) is large relative to the ravine width.

## Pipeline

```
DEM
 ├── ridge/spur mask  ──┐
 ├── hollow/valley mask ┴── extract ravine network (skeletonize + vectorize)
 │
 ├── per-segment geometry along axis:
 │      length_m, axis_drop_m, gradient
 │      max_drop within {10, 30, 100} m windows
 │
 ├── per-segment cross-section (perpendicular to axis):
 │      local_relief_m  = ridge_elev − axis_elev (left + right)
 │      ravine_width_m  = ridge-to-ridge distance
 │      narrowness      = local_relief / ravine_width  (depth-to-width ratio)
 │
 └── score = f(local_relief, narrowness, gradient)
       filter score > τ → canyon_segments.gpkg
```

## Outputs

- `ravine_network.gpkg` — all segments with full attribute table
- `canyon_segments.gpkg` — filtered subset above score threshold
- `ravine_network.tif` — raster of the extracted axis (1 = axis, 0 = elsewhere)
- `summary.json` — counts, score histogram, threshold used

Same schema as `predict.py` produces today, so existing downstream tooling
(KML export, Folium overlay) keeps working.

## Backend decisions (still open)

1. **Ravine-network extraction backend** — see "Backend tradeoffs" section
   below. Candidates: GRASS r.geomorphon (already integrated), multi-scale
   TPI, plan curvature, or a hybrid.
2. **Per-segment cross-section sampler** — sample N pixels left/right of axis
   along the perpendicular at each vertex; take the percentile of the highest
   nearby ridge pixel as the local ridge elevation.
3. **Scoring** — start with a transparent rule (`local_relief > 10 m` AND
   `narrowness > 0.3` AND `gradient > 0.05`), expose all three thresholds as
   CLI flags. Move to a composite score later if needed.
4. **Where it lives** — new module `src/canyongis/canyon_extract.py` + CLI
   `canyongis-canyon-extract`. Reuses `geomorphons.py`, `terrain.py`,
   `hydrology.py` primitives.

## Validation

Run on the same DEMs the U-Net was tested on. Compare:

- Recall against Ropewiki descent KMLs (do the labeled tracks fall on
  extracted canyon segments?).
- False-positive density on flat areas.
- Plot canyon segments over the DEM hillshade in QGIS for spot checks.

## What this replaces

- `canyongis-ml-predict` is not removed — the U-Net stays available for
  comparison and for cases where supervised signal is dense (e.g. future
  better labels).
- The new CLI is an *alternative* path with no training step.

## Out of scope (for now)

- Rappel-point detection. The U-Net's rappel head still has value because
  rappel locations are point events with relatively complete labels. We can
  layer rappel detection on top of geometric canyon segments later.

"""Reach-level evaluator and canyon ranker (DETECTION_STRATEGY.md Steps 0-2).

For every manifest mosaic: build (or load cached) drainage reaches with features
(:mod:`canyongis.reaches`), label them from *all* manifest descent tracks that fall
inside the mosaic, optionally refine labels with Ropewiki metadata, then score:

- every single feature as a ranker (sanity baselines),
- leave-one-mosaic-out (LOMO) learned rankers (logistic regression and
  histogram gradient boosting); the first model is "the ranker" for outputs,
- any external probability raster (e.g. U-Net ``canyon_prob.tif``) via ``--raster``,
- learned scores smoothed along the main stem (``--smooth-hops``),
- profile-step rappel candidates (D8 drops and isotonic-profile steps) against KML
  rappel points and Ropewiki ``Has number of rappels`` / ``Has longest rappel``.

``--extra-train-manifest`` mosaics are trained on but never scored, so a larger training
set can be compared on the same held-out terrain. ``--label-overrides`` applies reviewed
labels; ``review/fp_runs.*`` lists the top-scoring negative runs to review.

Metrics are length-weighted over reaches with label 0/1 (ignore = -1 excluded):
ROC AUC, average precision, recall at precision 0.5, precision at recall 0.8.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.windows
from PIL import Image
from rasterio.crs import CRS
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window
from scipy import ndimage as ndi
from scipy.stats import spearmanr
from shapely.geometry import MultiLineString
from shapely.ops import linemerge, substring
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .io import log
from .reaches import (
    CoarseArea,
    MosaicReaches,
    ProfileConfig,
    ReachConfig,
    TileSpec,
    compute_coarse_area,
    label_reaches,
    load_or_build_reaches,
    main_stem_down,
    plan_tiles,
    smooth_scores,
)
from .validate import parse_kml

DEFAULT_RANKER_FEATURES: tuple[str, ...] = (
    "log_area",
    "depth50",
    "depth150",
    "depth400",
    "wall25",
    "wall50",
    "wall100",
    "log_width5",
    "log_width20",
    "wall50_per_width20",
    "gradient",
    "max_drop",
    "max_step_excess",
    "drops_per_km",
    "ctx_depth150",
    "ctx_wall25",
    "ctx_wall50",
    "ctx_width5",
    "ctx_width20",
    "ctx_gradient",
    "ctx_max_drop",
    "ctx_max_step_excess",
    "ctx_drops_per_km",
)

# Isotonic-profile step features (reaches.ProfileConfig). Not in the default ranker set:
# they size and count rappels well but don't separate canyons from steep gullies
# (single-feature AUC 0.48-0.58, no ranker gain; DETECTION_STRATEGY.md §9). Opt in with
# --features.
PROFILE_RANKER_FEATURES: tuple[str, ...] = (
    "iso_max_step",
    "iso_steps_per_km",
    "iso_steep_frac",
    "iso_max_step_rel",
    "iso_steps_per_km_rel",
    "iso_steep_frac_rel",
    "ctx_iso_max_step",
    "ctx_iso_steps_per_km",
    "ctx_iso_steep_frac",
)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def rank_metrics(score: np.ndarray, label: np.ndarray, weight: np.ndarray) -> dict[str, float]:
    """Length-weighted ranking metrics over reaches with label in {0, 1}."""
    m = (label >= 0) & np.isfinite(score)
    s, y, w = score[m], (label[m] == 1).astype(np.int8), weight[m].astype(np.float64)
    wp, wn = w[y == 1].sum(), w[y == 0].sum()
    out = {"pos_km": wp / 1000.0, "neg_km": wn / 1000.0, "n_reaches": int(m.sum())}
    if wp <= 0 or wn <= 0:
        return {**out, "auc": math.nan, "ap": math.nan, "r_at_p50": math.nan, "p_at_r80": math.nan}
    precision, recall, _ = precision_recall_curve(y, s, sample_weight=w)
    ok = precision >= 0.5
    at80 = recall >= 0.8
    return {
        **out,
        "auc": float(roc_auc_score(y, s, sample_weight=w)),
        "ap": float(average_precision_score(y, s, sample_weight=w)),
        "r_at_p50": float(recall[ok].max()) if ok.any() else 0.0,
        "p_at_r80": float(precision[at80].max()) if at80.any() else 0.0,
    }


def best_f1_threshold(score: np.ndarray, label: np.ndarray, weight: np.ndarray) -> float:
    m = (label >= 0) & np.isfinite(score)
    precision, recall, thresholds = precision_recall_curve(
        label[m] == 1, score[m], sample_weight=weight[m]
    )
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-12)
    return float(thresholds[int(np.argmax(f1[:-1]))]) if len(thresholds) else math.inf


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

MODELS: tuple[str, ...] = ("logreg", "gbt")


def make_model(kind: str):
    """Class-balanced reach classifier. NaN features are handled by both models."""
    if kind == "logreg":
        return make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000),
        )
    if kind == "gbt":
        return HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.05,
            max_leaf_nodes=15,
            min_samples_leaf=40,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=0,
        )
    raise ValueError(f"unknown model {kind!r}")


def _fit(kind: str, X: np.ndarray, y: np.ndarray, w: np.ndarray):
    model = make_model(kind)
    if kind == "logreg":
        model.fit(X, y, logisticregression__sample_weight=w)
    else:
        model.fit(X, y, sample_weight=w)
    return model


def logreg_coefficients(model, names: list[str]) -> dict[str, float]:
    coef = model.named_steps["logisticregression"].coef_[0]
    return {n: float(c) for n, c in zip(names, coef, strict=True)}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def kml_title(path: str | Path) -> str:
    """Ropewiki page title from a synced KML filename (``Lytle_Creek_%28South_Fork%29``)."""
    return urllib.parse.unquote(Path(path).stem).replace("_", " ")


def _kml_bounds(kml: Path, crs: CRS) -> tuple[float, float, float, float] | None:
    lines = parse_kml(kml).descent_lines
    if lines.empty:
        return None
    return tuple(lines.to_crs(crs).total_bounds)


@dataclass
class MosaicInput:
    dem_path: Path
    bounds: tuple[float, float, float, float]  # (left, bottom, right, top) in DEM CRS
    crs: CRS
    positive_kmls: list[Path] = field(default_factory=list)
    ignore_kmls: list[Path] = field(default_factory=list)
    # Technical-section lines that replace a positive KML's full descent for labeling.
    positive_tracks: dict[Path, gpd.GeoDataFrame] = field(default_factory=dict)
    ignore_points_xy: list[tuple[float, float]] = field(default_factory=list)
    ropewiki: dict[str, dict[str, Any]] = field(default_factory=dict)
    tile: TileSpec | None = None
    core_bounds: tuple[float, float, float, float] | None = None  # tile core; None = bounds
    group: str = ""
    train_only: bool = False  # from --extra-train-manifest: trained on, never scored

    @property
    def name(self) -> str:
        return self.dem_path.stem + (self.tile.suffix if self.tile else "")


def _ropewiki_for_bounds(
    name: str, bounds_wgs84: tuple[float, float, float, float], cache_dir: Path, titles: set[str]
) -> dict[str, dict[str, Any]]:
    """Cached Ropewiki canyon properties inside a bbox (plus ``titles``), keyed by title."""
    from .ingest import query_ropewiki_canyon_props

    path = cache_dir / "ropewiki" / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    props = query_ropewiki_canyon_props(bounds_wgs84=bounds_wgs84)
    missing = sorted(titles - set(props))
    if missing:
        props.update(query_ropewiki_canyon_props(titles=missing))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(props, indent=1, sort_keys=True))
    return props


def download_ropewiki_kml(url: str, cache_dir: Path) -> Path | None:
    """Download a Ropewiki KML; None if it fails or isn't parseable KML (some
    ``Has KML file`` values point at external converters that return nothing)."""
    from .ingest import _download_file

    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    name = "".join(ch if ch.isalnum() or ch in "._-()" else "_" for ch in name)
    path = cache_dir / "ropewiki" / "kml" / name
    bad = path.with_name(path.name + ".bad")
    if bad.exists():
        return None
    try:
        if not path.exists():
            _download_file(url, path)
        parse_kml(path)
    except Exception as exc:  # network/parse errors shouldn't abort an eval
        log(f"reach-eval: unusable KML {url}: {exc}")
        path.unlink(missing_ok=True)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(f"{url}\n{exc}\n")
        return None
    return path


def _is_technical(props: dict[str, Any] | None) -> bool | None:
    rating = (props or {}).get("Has technical rating")
    try:
        return float(rating) >= 3 if rating is not None else None
    except (TypeError, ValueError):
        return None


def _too_few_rappels(props: dict[str, Any] | None, min_rappels: int) -> bool:
    n = (props or {}).get("Has number of rappels")
    return min_rappels > 0 and isinstance(n, float) and n < min_rappels


def _apply_ropewiki(
    mi: MosaicInput,
    props: dict[str, dict[str, Any]],
    labeled_titles: set[str],
    cache_dir: Path,
    nontechnical: str,
    min_rappels: int = 0,
) -> list[dict[str, Any]]:
    """Refine labels in place; returns notes on every change.

    - manifest track rated non-technical (< 3) → ignore (with ``nontechnical=ignore``)
    - track whose Ropewiki rappel count is known and < ``min_rappels`` → ignore (long
      creek walks with no rappels otherwise teach "big open creek = canyon")
    - unlabeled Ropewiki canyon with a KML: rated technical → positive, else ignore
    - unlabeled Ropewiki canyon without a KML → ignore radius around its coordinates
    """
    from pyproj import Transformer

    notes = []
    mi.ropewiki = props
    for k in list(mi.positive_kmls):
        p = props.get(kml_title(k))
        action = None
        if _is_technical(p) is False and nontechnical == "ignore":
            action = "nontechnical→ignore"
        elif _too_few_rappels(p, min_rappels):
            action = f"rappels < {min_rappels}→ignore"
        if action:
            mi.positive_kmls.remove(k)
            mi.ignore_kmls.append(k)
            notes.append({"mosaic": mi.name, "title": kml_title(k), "action": action})
    tf = Transformer.from_crs("EPSG:4326", mi.crs, always_xy=True)
    for title, p in props.items():
        if title in labeled_titles:
            continue
        url = p.get("Has KML file")
        has_url = isinstance(url, str) and url.startswith("http")
        kml = download_ropewiki_kml(url, cache_dir) if has_url else None
        if kml is not None and _is_technical(p) is True and not _too_few_rappels(p, min_rappels):
            mi.positive_kmls.append(kml)
            action = "unlabeled technical canyon (KML)→positive"
        elif kml is not None:
            mi.ignore_kmls.append(kml)
            action = "unlabeled canyon, not rated technical (KML)→ignore"
        elif p.get("Has latitude") is not None and p.get("Has longitude") is not None:
            mi.ignore_points_xy.append(tf.transform(p["Has longitude"], p["Has latitude"]))
            action = "unlabeled canyon (no KML)→ignore radius"
        else:
            continue
        notes.append({"mosaic": mi.name, "title": title, "action": action})
    return notes


@dataclass
class TechnicalSection:
    """Where a descent track's technical part is, from its named rappel points."""

    kml: Path
    n_anchors: int  # rappel points within ``snap_m`` of the descent line
    lines: gpd.GeoDataFrame | None  # WGS84 positive section; None = unmarked
    full_km: float
    section_km: float


def technical_section(
    kml: Path,
    ropewiki_rappels: float | None,
    *,
    margin_m: float = 200.0,
    snap_m: float = 75.0,
) -> TechnicalSection:
    """Descent-line stretch from ``margin_m`` before the first named rappel to
    ``margin_m`` after the last one (per connected line part).

    Counts as marked with ≥ 2 rappel points, or 1 when Ropewiki lists ≤ 1 rappel; a
    lone "R1" on a multi-rappel canyon doesn't say where the technical part ends.
    """
    feats = parse_kml(kml)
    lines = feats.descent_lines
    if lines.empty and not feats.anchor_points().empty:
        # Some KMLs name the main track oddly ("", "O'Melveny Cyn"); the rappel points
        # locate the section, so use unclassified lines (never approach/exit ones).
        lines = feats.unclassified_lines
    if lines.empty:
        return TechnicalSection(kml, 0, None, 0.0, 0.0)
    utm = lines.estimate_utm_crs()
    # Join parts only at shared endpoints: union_all() would split a self-crossing GPS
    # track at every crossing, leaving rappel points snapped to tiny fragments.
    geoms = list(lines.to_crs(utm).explode(index_parts=False).geometry)
    merged = linemerge(MultiLineString(geoms)) if len(geoms) > 1 else geoms[0]
    parts = list(getattr(merged, "geoms", [merged]))
    full_km = sum(p.length for p in parts) / 1000.0
    anchors = feats.anchor_points()
    hits: dict[int, list[float]] = {}
    for pt in anchors.to_crs(utm).geometry if not anchors.empty else []:
        d = [part.distance(pt) for part in parts]
        i = int(np.argmin(d))
        if d[i] <= snap_m:
            hits.setdefault(i, []).append(parts[i].project(pt))
    n = sum(len(v) for v in hits.values())
    marked = n >= 2 or (n == 1 and ropewiki_rappels is not None and ropewiki_rappels <= 1)
    if not marked:
        return TechnicalSection(kml, n, None, full_km, 0.0)
    pieces = [
        substring(parts[i], max(0.0, min(ds) - margin_m), min(parts[i].length, max(ds) + margin_m))
        for i, ds in hits.items()
    ]
    gdf = gpd.GeoDataFrame(geometry=pieces, crs=utm).to_crs("EPSG:4326")
    gdf.attrs["key"] = str(kml)
    return TechnicalSection(kml, n, gdf, full_km, sum(p.length for p in pieces) / 1000.0)


def _apply_technical_sections(
    mi: MosaicInput,
    unmarked: str,
    sections: dict[Path, TechnicalSection],
    margin_m: float,
) -> None:
    """Label only the technical section of each positive track: the section is
    positive, the rest of the descent is ignored. Unmarked tracks stay whole
    (``unmarked="whole"``) or are ignored entirely (``unmarked="ignore"``)."""
    for k in list(mi.positive_kmls):
        if k not in sections:
            n_rap = (mi.ropewiki.get(kml_title(k)) or {}).get("Has number of rappels")
            sections[k] = technical_section(
                k, n_rap if isinstance(n_rap, float) else None, margin_m=margin_m
            )
        sec = sections[k]
        if sec.lines is not None:
            mi.positive_tracks[k] = sec.lines
            mi.ignore_kmls.append(k)  # rest of the descent: positives take precedence
        elif unmarked == "ignore":
            mi.positive_kmls.remove(k)
            mi.ignore_kmls.append(k)


def _assign_groups(inputs: list[MosaicInput]) -> None:
    """Group inputs whose extents overlap (union-find), so CV never trains on
    terrain it tests on. Tiles of one DEM always share a group."""
    parent = list(range(len(inputs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    wgs = [transform_bounds(mi.crs, "EPSG:4326", *mi.bounds) for mi in inputs]
    for i in range(len(inputs)):
        for j in range(i + 1, len(inputs)):
            a, b = wgs[i], wgs[j]
            same_dem = inputs[i].dem_path == inputs[j].dem_path
            if same_dem or (a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]):
                parent[find(i)] = find(j)
    roots: dict[int, str] = {}
    for i, mi in enumerate(inputs):
        mi.group = roots.setdefault(find(i), mi.dem_path.stem)


def build_inputs(
    manifest_path: Path,
    root: Path,
    cache_dir: Path,
    *,
    max_pixels: float,
    tile_px: int,
    tile_overlap_px: int,
    only: list[str],
    use_ropewiki: bool,
    nontechnical: str,
    min_rappels: int = 0,
    technical: str = "off",
    unmarked: str = "whole",
    technical_margin_m: float = 200.0,
    sections: dict[Path, TechnicalSection] | None = None,
    extra_train_manifest: Path | None = None,
    exclude_tracks: tuple[str, ...] = (),
) -> tuple[list[MosaicInput], list[dict[str, Any]]]:
    """Resolve mosaics (tiling ones larger than ``max_pixels`` when ``tile_px`` > 0)
    and their label sources. Returns (inputs, label-source notes).

    Entries of ``extra_train_manifest`` (paths relative to its own directory) become
    ``train_only`` inputs; their tracks label every mosaic they touch, like the rest.
    Tracks whose Ropewiki title is in ``exclude_tracks`` are ignored, never positive."""
    excluded = {t.casefold() for t in exclude_tracks}
    entries = [{**e, "_train_only": False} for e in json.loads(manifest_path.read_text())]
    if extra_train_manifest is not None:
        extra_root = extra_train_manifest.parent
        for e in json.loads(extra_train_manifest.read_text()):
            kmls = e.get("kml_paths", [e.get("kml_path")])
            entries.append(
                {
                    "dem_path": str(extra_root / e["dem_path"]),
                    "kml_paths": [str(extra_root / k) for k in kmls if k],
                    "_train_only": True,
                }
            )
    all_kmls = sorted(
        {root / k for e in entries for k in e.get("kml_paths", [e.get("kml_path")]) if k}
    )
    labeled_titles = {kml_title(k) for k in all_kmls}
    kml_bounds: dict[str, dict[Path, tuple | None]] = {}
    notes: list[dict[str, Any]] = []
    inputs: list[MosaicInput] = []
    seen: set[Path] = set()
    for e in entries:
        dem = root / e["dem_path"]
        if dem in seen or (only and not any(s in dem.name for s in only)):
            continue
        seen.add(dem)
        with rasterio.open(dem) as src:
            crs = src.crs
            if src.width * src.height <= max_pixels:
                parts = [(None, tuple(src.bounds), tuple(src.bounds))]
            elif tile_px > 0:
                parts = []
                for t in plan_tiles(src.height, src.width, tile_px, tile_overlap_px):
                    dec = 16
                    mask = src.read_masks(
                        1,
                        window=t.window,
                        out_shape=(max(1, t.height // dec), max(1, t.width // dec)),
                    )
                    if (mask > 0).mean() < 0.2:
                        continue
                    core = Window(
                        t.col_off + t.core_cols[0],
                        t.row_off + t.core_rows[0],
                        t.core_cols[1] - t.core_cols[0],
                        t.core_rows[1] - t.core_rows[0],
                    )
                    parts.append(
                        (
                            t,
                            tuple(rasterio.windows.bounds(t.window, src.transform)),
                            tuple(rasterio.windows.bounds(core, src.transform)),
                        )
                    )
                log(f"reach-eval: tiling {dem.name} into {len(parts)} tiles")
            else:
                log(f"reach-eval: skipping {dem.name} ({src.width}x{src.height} > max-pixels)")
                continue
        crs_key = crs.to_string()
        if crs_key not in kml_bounds:
            kml_bounds[crs_key] = {k: _kml_bounds(k, crs) for k in all_kmls}
        for tile, b, core_b in parts:
            mi = MosaicInput(
                dem_path=dem,
                bounds=b,
                crs=crs,
                tile=tile,
                core_bounds=core_b,
                train_only=e["_train_only"],
            )
            # Every manifest track touching this extent labels it, not just its own entry.
            for k, kb in kml_bounds[crs_key].items():
                if kb and kb[0] < b[2] and kb[2] > b[0] and kb[1] < b[3] and kb[3] > b[1]:
                    mi.positive_kmls.append(k)
            if use_ropewiki:
                props = _ropewiki_for_bounds(
                    mi.name,
                    transform_bounds(crs, "EPSG:4326", *b),
                    cache_dir,
                    {kml_title(k) for k in mi.positive_kmls},
                )
                notes += _apply_ropewiki(
                    mi, props, labeled_titles, cache_dir, nontechnical, min_rappels
                )
            for k in [k for k in mi.positive_kmls if kml_title(k).casefold() in excluded]:
                mi.positive_kmls.remove(k)
                mi.ignore_kmls.append(k)
                notes.append(
                    {"mosaic": mi.name, "title": kml_title(k), "action": "excluded→ignore"}
                )
            if technical != "off":
                _apply_technical_sections(mi, unmarked, sections, technical_margin_m)
            inputs.append(mi)
    _assign_groups(inputs)
    return inputs, notes


# ---------------------------------------------------------------------------
# External rasters
# ---------------------------------------------------------------------------


def sample_raster_on_reaches(
    raster_path: Path, mr: MosaicReaches, buffer_m: float = 5.0
) -> np.ndarray:
    """Per-reach mean of a raster's local max (``buffer_m``) along reach pixels.
    NaN where the raster doesn't cover the reach."""
    dst = np.full(mr.shape, np.nan, dtype=np.float32)
    with rasterio.open(raster_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            dst_transform=mr.affine,
            dst_crs=CRS.from_wkt(mr.crs_wkt),
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    if not np.isfinite(dst).any():
        return np.full(mr.n, np.nan, dtype=np.float32)
    size = 2 * max(1, round(buffer_m / mr.pixel_size)) + 1
    covered = np.isfinite(dst)
    local = ndi.maximum_filter(np.where(covered, dst, -np.inf), size=size)
    v = np.where(covered[mr.pix_r, mr.pix_c], local[mr.pix_r, mr.pix_c], np.nan).astype(np.float64)
    sizes = np.diff(mr.reach_ptr)
    sums = np.add.reduceat(np.nan_to_num(v), mr.reach_ptr[:-1])
    cnt = np.add.reduceat(np.isfinite(v).astype(np.float64), mr.reach_ptr[:-1])
    out = np.where(cnt >= 0.5 * sizes, sums / np.maximum(cnt, 1), np.nan)
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Rappel candidates
# ---------------------------------------------------------------------------


def _step_candidates(mr: MosaicReaches, kind: str):
    """(rows, cols, size, reach) of rappel candidates: ``d8`` = raw-profile drops sized
    by excess over the local grade; ``iso`` = isotonic-profile steps sized by height."""
    if kind == "d8":
        return mr.drop_r, mr.drop_c, mr.drop_excess_m, mr.drop_reach
    if kind == "iso":
        return mr.step_r, mr.step_c, mr.step_h, mr.step_reach
    raise ValueError(f"unknown candidate kind {kind!r}")


RAPPEL_THRESHOLDS = {"d8": (0.0, 2.0, 4.0, 8.0), "iso": (3.0, 5.0, 8.0, 12.0)}


def evaluate_rappels(
    mosaics: list[tuple[MosaicInput, MosaicReaches]],
    excess_thresholds: tuple[float, ...] | None = None,
    match_m: float = 25.0,
    track_tol_m: float = 20.0,
    kind: str = "d8",
) -> dict[str, Any]:
    """Profile-step candidates on positive reaches vs KML rappel points and Ropewiki
    per-canyon rappel counts / longest rappel. Thresholds apply to the candidate size
    (``excess`` for d8 drops, ``height`` for iso steps).

    Each KML is scored once, on the input whose core holds most of its track (tiles
    and overlapping mosaics would otherwise count it several times). Train-only inputs
    are skipped.
    """
    thresholds = excess_thresholds or RAPPEL_THRESHOLDS[kind]
    size_name = "excess" if kind == "d8" else "height"
    from pyproj import Transformer
    from shapely import distance as shp_distance
    from shapely import points as shp_points
    from shapely.geometry import box

    tracks: dict[tuple[Path, str], Any] = {}
    best: dict[Path, tuple[float, int]] = {}
    for idx, (mi, mr) in enumerate(mosaics):
        if mi.train_only:
            continue
        core = box(*(mi.core_bounds or mi.bounds))
        for k in mi.positive_kmls:
            key = (k, mr.crs_wkt)
            if key not in tracks:
                lines = parse_kml(k).descent_lines
                tracks[key] = None if lines.empty else lines.to_crs(mr.crs_wkt).union_all()
            track = tracks[key]
            inside = track.intersection(core).length if track is not None else 0.0
            if inside > best.get(k, (0.0, -1))[0]:
                best[k] = (inside, idx)

    point_rows: list[dict[str, Any]] = []
    canyon_rows: list[dict[str, Any]] = []
    for k, (_, idx) in sorted(best.items()):
        mi, mr = mosaics[idx]
        track = tracks[(k, mr.crs_wkt)]
        cr, cc, size, creach = _step_candidates(mr, kind)
        xs = mr.affine.c + (cc + 0.5) * mr.affine.a
        ys = mr.affine.f + (cr + 0.5) * mr.affine.e
        cand = shp_points(np.column_stack([xs, ys])) if len(xs) else np.zeros(0, dtype=object)
        on_pos = mr.label[creach] == 1
        tf = Transformer.from_crs("EPSG:4326", mr.crs_wkt, always_xy=True)
        for g in parse_kml(k).anchor_points().geometry:
            pt = shp_points(*tf.transform(g.x, g.y))
            row = {"mosaic": mr.stem, "kml": kml_title(k)}
            for t in thresholds:
                sel = on_pos & (size >= t)
                row[f"nearest_m@{size_name}{t:g}"] = (
                    float(shp_distance(cand[sel], pt).min()) if sel.any() else math.inf
                )
            point_rows.append(row)

        near_track = (
            shp_distance(cand, track) <= track_tol_m if len(cand) else np.zeros(0, dtype=bool)
        )
        props = mi.ropewiki.get(kml_title(k), {})
        row = {
            "mosaic": mr.stem,
            "kml": kml_title(k),
            "track_km": float(track.length / 1000.0),
            "ropewiki_rappels": props.get("Has number of rappels"),
            "ropewiki_longest_m": props.get("Has longest rappel"),
            "max_drop_m": float(
                (mr.drop_m if kind == "d8" else size)[near_track].max() if near_track.any() else 0.0
            ),
        }
        for t in thresholds:
            row[f"n_steps@{size_name}{t:g}"] = int((near_track & (size >= t)).sum())
        canyon_rows.append(row)

    def spearman(pairs: list[tuple[Any, Any]]) -> float:
        pairs = [(a, b) for a, b in pairs if isinstance(a, float) and isinstance(b, float | int)]
        if len(pairs) < 3:
            return math.nan
        a = np.array(pairs, dtype=float)
        return float(spearmanr(a[:, 0], a[:, 1]).statistic)

    km = sum(r["track_km"] for r in canyon_rows)
    summary: dict[str, Any] = {
        "candidates": kind,
        "n_canyons": len(canyon_rows),
        "n_kml_rappel_points": len(point_rows),
        "spearman_longest_vs_max_drop": spearman(
            [(r["ropewiki_longest_m"], r["max_drop_m"]) for r in canyon_rows]
        ),
    }
    for t in thresholds:
        d = np.array([r[f"nearest_m@{size_name}{t:g}"] for r in point_rows])
        summary[f"{size_name}>={t:g}m"] = {
            f"kml_point_recall@{match_m:g}m": float((d <= match_m).mean()) if len(d) else math.nan,
            "steps_per_track_km": (
                sum(r[f"n_steps@{size_name}{t:g}"] for r in canyon_rows) / km if km else math.nan
            ),
            "spearman_vs_ropewiki_rappels": spearman(
                [(r["ropewiki_rappels"], r[f"n_steps@{size_name}{t:g}"]) for r in canyon_rows]
            ),
        }
    return {"summary": summary, "points": point_rows, "canyons": canyon_rows}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_outcome_png(
    mr: MosaicReaches, score: np.ndarray, threshold: float, path: Path, max_px: int = 2000
) -> None:
    """Hillshade with reaches colored TP green / FP red / FN blue / ignore gray."""
    from .ravine_compare import compute_hillshade

    with rasterio.open(mr.dem_path) as src:
        col_off, row_off = ~src.transform * (mr.affine.c, mr.affine.f)
        win = Window(round(col_off), round(row_off), mr.shape[1], mr.shape[0])
        elev = src.read(1, window=win, masked=True).filled(np.nan).astype(np.float32)
    shade = compute_hillshade(elev, mr.pixel_size)
    rgb = np.repeat(
        (shade.astype(np.float32) * 0.8 + 30).clip(0, 255).astype(np.uint8)[..., None], 3, -1
    )
    pred = score >= threshold
    colors = {
        "ignore": ((mr.label == -1), (150, 150, 150)),
        "tn": ((mr.label == 0) & ~pred, (230, 230, 120)),
        "fn": ((mr.label == 1) & ~pred, (40, 90, 255)),
        "fp": ((mr.label == 0) & pred, (255, 40, 40)),
        "tp": ((mr.label == 1) & pred, (30, 220, 60)),
    }
    pix_reach = mr.per_pixel_reach()
    dil = max(1, round(max(mr.shape) / max_px))
    for name, (sel, color) in colors.items():
        mask = np.zeros(mr.shape, dtype=bool)
        on = sel[pix_reach]
        mask[mr.pix_r[on], mr.pix_c[on]] = True
        if name != "tn":
            mask = ndi.binary_dilation(mask, iterations=dil)
        rgb[mask] = color
    img = Image.fromarray(rgb)
    scale = max_px / max(mr.shape)
    if scale < 1:
        img = img.resize((round(mr.shape[1] * scale), round(mr.shape[0] * scale)), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return "nan" if math.isnan(v) else f"{v:.3f}"
    return str(v)


def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_fmt(r.get(c, "")) for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def _coarse_area_cached(dem_path: Path, cfg: ReachConfig, cache_dir: Path) -> CoarseArea:
    path = cache_dir / "coarse" / f"{dem_path.stem}.{cfg.signature()}.npz"
    if path.exists():
        with np.load(path) as z:
            return CoarseArea(area_m2=z["area_m2"], transform=tuple(z["transform"]))
    log(f"reach-eval: coarse drainage area for {dem_path.name}")
    ca = compute_coarse_area(dem_path, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, area_m2=ca.area_m2, transform=np.array(ca.transform))
    return ca


def area_matched_labels(
    mosaics: list[MosaicReaches],
    quantile: float = 0.1,
    labels: list[np.ndarray] | None = None,
) -> tuple[list[np.ndarray], float]:
    """Labels restricted to reaches at least as large as the ``quantile`` (length-
    weighted) of positive-reach ``log_area``. Removes the "it's the big drainage"
    shortcut: within this subset, area alone can no longer separate the classes.
    ``labels`` defaults to each mosaic's own ``label``."""
    labels = labels if labels is not None else [mr.label for mr in mosaics]
    la = np.concatenate([mr.features["log_area"] for mr in mosaics])
    lab = np.concatenate(labels)
    w = np.concatenate([mr.length_m for mr in mosaics])
    pos = lab == 1
    order = np.argsort(la[pos])
    cw = np.cumsum(w[pos][order])
    thr = float(la[pos][order][np.searchsorted(cw, quantile * cw[-1])]) if pos.any() else -np.inf
    return [
        np.where(mr.features["log_area"] >= thr, y, -1)
        for mr, y in zip(mosaics, labels, strict=True)
    ], thr


OVERRIDE_LABELS = {"positive": 1, "negative": 0, "ignore": -1}


def read_label_overrides(path: Path) -> gpd.GeoDataFrame:
    """Reviewed label overrides: any vector file with a ``label`` column (positive /
    negative / ignore) and an optional ``buffer_m`` (default 25) around each geometry."""
    gdf = gpd.read_file(path)
    if "label" not in gdf.columns:
        raise ValueError(f"{path}: overrides need a 'label' column")
    bad = sorted(set(gdf["label"]) - set(OVERRIDE_LABELS))
    if bad:
        raise ValueError(f"{path}: unknown override labels {bad}")
    if "buffer_m" not in gdf.columns:
        gdf["buffer_m"] = 25.0
    return gdf


def apply_label_overrides(mr: MosaicReaches, overrides: gpd.GeoDataFrame) -> int:
    """Set labels of core reaches inside override geometries (positive/negative: ≥ half
    the reach inside; ignore: any part). Applied after all other label rules; returns
    the number of reaches changed."""
    from shapely import contains_xy

    if overrides.empty:
        return 0
    ov = overrides.to_crs(mr.crs_wkt)
    tr = mr.affine
    xs = tr.c + (mr.pix_c + 0.5) * tr.a
    ys = tr.f + (mr.pix_r + 0.5) * tr.e
    sizes = np.maximum(np.diff(mr.reach_ptr), 1)
    changed = 0
    for geom, lab, buf in zip(ov.geometry, ov["label"], ov["buffer_m"], strict=True):
        if geom is None or geom.is_empty:
            continue
        inside = contains_xy(geom.buffer(float(buf)), xs, ys)
        if not inside.any():
            continue
        frac = np.add.reduceat(inside.astype(np.float64), mr.reach_ptr[:-1]) / sizes
        value = OVERRIDE_LABELS[lab]
        hit = (frac > 0 if value == -1 else frac >= 0.5) & mr.core
        changed += int((mr.label[hit] != value).sum())
        mr.label[hit] = value
    return changed


def false_positive_runs(
    mi: MosaicInput, mr: MosaicReaches, gdf: gpd.GeoDataFrame, score: np.ndarray, threshold: float
) -> list[dict[str, Any]]:
    """Main-stem runs of negative reaches scoring ≥ ``threshold``, for label review:
    each may be a real but unmapped canyon (→ override to positive/ignore) or a true
    ranker error. Includes the nearest Ropewiki canyon from the cached properties."""
    from pyproj import Transformer
    from shapely.ops import unary_union

    sel = (mr.label == 0) & (score >= threshold)
    if not sel.any():
        return []
    parent = np.arange(mr.n)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    dn = main_stem_down(mr.down.astype(np.int64), mr.main_up.astype(np.int64))
    for i in np.flatnonzero(sel & (dn >= 0)):
        if sel[dn[i]]:
            parent[find(i)] = find(int(dn[i]))
    runs: dict[int, list[int]] = {}
    for i in np.flatnonzero(sel).tolist():
        runs.setdefault(find(i), []).append(i)
    tf = Transformer.from_crs("EPSG:4326", mr.crs_wkt, always_xy=True)
    known = [
        (t, *tf.transform(p["Has longitude"], p["Has latitude"]))
        for t, p in mi.ropewiki.items()
        if p.get("Has latitude") is not None and p.get("Has longitude") is not None
    ]
    to_wgs = Transformer.from_crs(mr.crs_wkt, "EPSG:4326", always_xy=True)
    out = []
    for ids in runs.values():
        geom = unary_union(list(gdf.geometry.iloc[ids]))
        c = geom.centroid
        near = min(known, key=lambda k: math.hypot(k[1] - c.x, k[2] - c.y), default=None)
        lon, lat = to_wgs.transform(c.x, c.y)
        out.append(
            {
                "mosaic": mr.stem,
                "length_m": float(mr.length_m[ids].sum()),
                "mean_score": float(score[ids].mean()),
                "max_score": float(score[ids].max()),
                "max_log_area": float(mr.features["log_area"][ids].max()),
                "lat": lat,
                "lon": lon,
                "nearest_ropewiki": near[0] if near else "",
                "nearest_ropewiki_m": math.hypot(near[1] - c.x, near[2] - c.y)
                if near
                else math.nan,
                "geometry": geom,
                "crs": mr.crs_wkt,
            }
        )
    return out


def run_reach_eval(
    manifest_path: Path,
    out_dir: Path,
    *,
    root: Path | None = None,
    cache_dir: Path | None = None,
    cfg: ReachConfig | None = None,
    max_pixels: float = 60e6,
    tile_px: int = 0,
    tile_overlap_px: int = 750,
    only: list[str] | None = None,
    use_ropewiki: bool = False,
    nontechnical: str = "ignore",
    min_rappels: int = 0,
    balance_canyons: bool = False,
    technical: str = "markers",
    unmarked: str = "whole",
    technical_margin_m: float = 200.0,
    rasters: dict[str, str] | None = None,
    models: tuple[str, ...] = ("logreg", "gbt"),
    features: list[str] | None = None,
    write_png: bool = True,
    profile: ProfileConfig | None = None,
    smooth_hops: tuple[int, ...] = (),
    smooth_modes: tuple[str, ...] = ("median",),
    label_overrides: Path | None = None,
    extra_train_manifest: Path | None = None,
    exclude_tracks: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build/label/score every input; see the module docstring. With ``smooth_hops``,
    the first smoothing variant of the first model drives outputs and the threshold.

    Python defaults are the bare pipeline (no smoothing, no profile steps); the CLI
    defaults to ``--smooth-hops 1 --smooth median --profile``."""
    cfg = cfg or ReachConfig()
    root = root or manifest_path.parent
    cache_dir = cache_dir or (out_dir / "cache")
    out_dir.mkdir(parents=True, exist_ok=True)
    sections: dict[Path, TechnicalSection] = {}

    inputs, notes = build_inputs(
        manifest_path,
        root,
        cache_dir,
        max_pixels=max_pixels,
        tile_px=tile_px,
        tile_overlap_px=tile_overlap_px,
        only=only or [],
        use_ropewiki=use_ropewiki,
        nontechnical=nontechnical,
        min_rappels=min_rappels,
        technical=technical,
        unmarked=unmarked,
        technical_margin_m=technical_margin_m,
        sections=sections,
        extra_train_manifest=extra_train_manifest,
        exclude_tracks=exclude_tracks,
    )
    if not inputs:
        raise SystemExit("reach-eval: no mosaics to evaluate")
    mosaics: list[tuple[MosaicInput, MosaicReaches]] = []
    coarse: dict[Path, CoarseArea] = {}
    overrides = read_label_overrides(label_overrides) if label_overrides else None
    n_overridden = 0
    for mi in inputs:
        log(f"reach-eval: {mi.name} (group {mi.group})")
        ca = None
        if mi.tile is not None:
            if mi.dem_path not in coarse:
                coarse[mi.dem_path] = _coarse_area_cached(mi.dem_path, cfg, cache_dir)
            ca = coarse[mi.dem_path]
        mr = load_or_build_reaches(
            mi.dem_path, cfg, cache_dir / "reaches", tile=mi.tile, coarse_area=ca, profile=profile
        )
        mr.group = mi.group
        label_reaches(
            mr,
            [mi.positive_tracks.get(k, k) for k in mi.positive_kmls],
            cfg,
            ignore_kmls=mi.ignore_kmls,
            ignore_points_xy=mi.ignore_points_xy,
        )
        if overrides is not None:
            n_overridden += apply_label_overrides(mr, overrides)
        mosaics.append((mi, mr))
    mrs = [mr for _, mr in mosaics]

    names = list(features or [f for f in DEFAULT_RANKER_FEATURES if f in mrs[0].features])
    X = [np.column_stack([mr.features[f] for f in names]).astype(np.float64) for mr in mrs]
    Y = [mr.label for mr in mrs]
    # Scored labels: train-only inputs (--extra-train-manifest) never count in metrics.
    Y_eval = [
        np.full_like(y, -1) if mi.train_only else y for (mi, _), y in zip(mosaics, Y, strict=True)
    ]
    W = [mr.length_m.astype(np.float64) for mr in mrs]
    groups = sorted({mr.group for mr in mrs})
    W_train = _canyon_balanced_weights(mrs) if balance_canyons else W

    def train_on(idx: list[int], cols: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (
            np.vstack([X[j][Y[j] >= 0][:, cols] for j in idx]),
            np.concatenate([Y[j][Y[j] >= 0] for j in idx]).astype(np.int8),
            np.concatenate([W_train[j][Y[j] >= 0] for j in idx]),
        )

    def logo_scores(kind: str, cols: list[int]) -> list[np.ndarray]:
        """Leave-one-group-out: overlapping mosaics/tiles are held out together."""
        scores: list[np.ndarray] = [np.empty(0)] * len(mrs)
        for g in groups:
            test = [i for i, mr in enumerate(mrs) if mr.group == g]
            train = [i for i, mr in enumerate(mrs) if mr.group != g]
            fitted = _fit(kind, *train_on(train, cols))
            for i in test:
                scores[i] = fitted.predict_proba(X[i][:, cols])[:, 1]
        return scores

    all_cols = list(range(len(names)))
    methods: dict[str, list[np.ndarray]] = {}
    for kind in models:
        methods[f"{kind}_logo"] = logo_scores(kind, all_cols)
        log(f"reach-eval: {kind} leave-one-group-out done ({len(groups)} groups)")
    if "log_area" in names:
        no_area = [i for i, n in enumerate(names) if n != "log_area"]
        methods[f"{models[0]}_logo_no_log_area"] = logo_scores(models[0], no_area)
    ranker = methods[f"{models[0]}_logo"]
    smooth_names = []
    for hops in smooth_hops:
        for how in smooth_modes:
            for kind in models:
                name = f"{kind}_logo_smooth{hops}_{how}"
                methods[name] = [
                    smooth_scores(sc, mr.length_m, mr.down, mr.main_up, hops, how)
                    for sc, mr in zip(methods[f"{kind}_logo"], mrs, strict=True)
                ]
                if kind == models[0]:
                    smooth_names.append(name)
    if smooth_names:
        ranker = methods[smooth_names[0]]
    full_logreg = _fit("logreg", *train_on(list(range(len(mrs))), all_cols))
    for name, template in (rasters or {}).items():
        methods[f"raster:{name}"] = []
        for mr in mrs:
            path = Path(template.format(stem=mr.stem))
            methods[f"raster:{name}"].append(
                sample_raster_on_reaches(path, mr) if path.exists() else np.full(mr.n, np.nan)
            )

    Y_am, area_thr = area_matched_labels(mrs, labels=Y_eval)
    all_w = np.concatenate(W)

    def pooled(scores: list[np.ndarray], labels: list[np.ndarray]) -> dict[str, float]:
        return rank_metrics(np.concatenate(scores), np.concatenate(labels), all_w)

    def with_matched(scores: list[np.ndarray], labels=Y_eval) -> dict[str, float]:
        m = pooled(scores, labels)
        am = pooled(scores, [np.where(ya >= 0, y, -1) for y, ya in zip(labels, Y_am, strict=True)])
        return {**m, "auc_am": am["auc"], "ap_am": am["ap"], "base_rate_am": _base_rate(am)}

    method_rows = []
    for name, scores in methods.items():
        covered = [
            i
            for i, sc in enumerate(scores)
            if np.isfinite(sc).any() and not mosaics[i][0].train_only
        ]
        row = {"method": name, "mosaics": len(covered), **with_matched(scores)}
        per = [rank_metrics(scores[i], Y_eval[i], W[i])["ap"] for i in covered]
        row["median_mosaic_ap"] = float(np.nanmedian(per)) if per else math.nan
        method_rows.append(row)
        if name.startswith("raster:") and covered:
            # Same reaches as the raster scored (partial coverage leaves NaNs).
            same = [np.where(np.isfinite(sc), y, -1) for sc, y in zip(scores, Y_eval, strict=True)]
            sub = with_matched(ranker, same)
            method_rows.append({"method": f"  {models[0]} on the same reaches", **sub})

    feature_rows = []
    for f in sorted(mrs[0].features):
        feature_rows.append(
            {"feature": f, **with_matched([mr.features[f].astype(np.float64) for mr in mrs])}
        )
    feature_rows.sort(key=lambda r: -abs(r["auc"] - 0.5) if not math.isnan(r["auc"]) else 0)

    mosaic_rows = []
    for i, (mi, mr) in enumerate(mosaics):
        m = rank_metrics(ranker[i], Y[i], W[i])
        mosaic_rows.append(
            {
                "mosaic": mr.stem,
                "group": mr.group,
                "train_only": mi.train_only,
                "reaches": mr.n,
                "pos_kmls": len(mi.positive_kmls),
                "ignore_kmls": len(mi.ignore_kmls),
                "ignore_pts": len(mi.ignore_points_xy),
                **{k: v for k, v in m.items() if k != "n_reaches"},
                "ap_am": rank_metrics(ranker[i], Y_am[i], W[i])["ap"],
                "auc_log_area": rank_metrics(mr.features["log_area"], Y[i], W[i])["auc"],
            }
        )

    rappels = evaluate_rappels(mosaics)
    rappels_iso = evaluate_rappels(mosaics, kind="iso") if profile else None

    # Per-mosaic outputs at the pooled F1-optimal ranker threshold.
    threshold = best_f1_threshold(np.concatenate(ranker), np.concatenate(Y_eval), all_w)
    fp_rows: list[dict[str, Any]] = []
    for i, (mi, mr) in enumerate(mosaics):
        cols = {"ranker_score": ranker[i].astype(np.float32)}
        if smooth_names:
            cols["ranker_score_raw"] = methods[f"{models[0]}_logo"][i].astype(np.float32)
        for name, scores in methods.items():
            if name.startswith("raster:"):
                cols[name.removeprefix("raster:")] = scores[i].astype(np.float32)
        gdf = mr.to_gdf(cols)
        gdf["predicted"] = ranker[i] >= threshold
        gpkg = out_dir / "reaches" / f"{mr.stem}.gpkg"
        gpkg.parent.mkdir(parents=True, exist_ok=True)
        gpkg.unlink(missing_ok=True)
        gdf.to_file(gpkg, layer="reaches", driver="GPKG")
        if len(mr.drop_r):
            mr.drops_gdf().to_file(gpkg, layer="steps", driver="GPKG")
        if len(mr.step_r):
            mr.iso_steps_gdf().to_file(gpkg, layer="iso_steps", driver="GPKG")
        if write_png:
            render_outcome_png(mr, ranker[i], threshold, out_dir / "png" / f"{mr.stem}.png")
        if not mi.train_only:
            fp_rows += false_positive_runs(mi, mr, gdf, ranker[i], threshold)
    _write_fp_review(fp_rows, out_dir / "review")

    snap_stats: dict[str, dict[str, Any]] = {}
    for mr in mrs:
        for st in mr.snap_stats:
            best = snap_stats.get(st["track"])
            if st["points"] and (best is None or st["matched_frac"] > best["matched_frac"]):
                snap_stats[st["track"]] = {**st, "track": kml_title(st["track"])}

    result = {
        "config": asdict(cfg),
        "min_rappels": min_rappels,
        "balance_canyons": balance_canyons,
        "technical": technical,
        "unmarked": unmarked,
        "technical_sections": [
            {
                "kml": kml_title(t.kml),
                "rappel_points": t.n_anchors,
                "marked": t.lines is not None,
                "full_km": t.full_km,
                "section_km": t.section_km,
            }
            for t in sorted(sections.values(), key=lambda t: kml_title(t.kml))
        ],
        "models": list(models),
        "features": names,
        "profile": asdict(profile) if profile else None,
        "smoothing": {
            "hops": list(smooth_hops),
            "modes": list(smooth_modes),
            "ranker": (smooth_names[0] if smooth_names else f"{models[0]}_logo"),
        },
        "label_overrides": {"path": str(label_overrides or ""), "reaches_changed": n_overridden},
        "train_only": sorted({mi.name for mi, _ in mosaics if mi.train_only}),
        "snap_stats": sorted(snap_stats.values(), key=lambda r: r["track"]),
        "fp_runs": len(fp_rows),
        "groups": groups,
        "area_matched_log_area_min": area_thr,
        "threshold_f1": threshold,
        "methods": method_rows,
        "single_features": feature_rows,
        "mosaics": mosaic_rows,
        "coefficients_full_fit": logreg_coefficients(full_logreg, names),
        "rappels": rappels,
        "rappels_iso": rappels_iso,
        "label_notes": notes,
    }
    (out_dir / "metrics.json").write_text(json.dumps(result, indent=1, default=float))
    (out_dir / "report.md").write_text(_report_md(result))
    log(f"reach-eval: wrote {out_dir / 'report.md'}")
    print(_report_md(result, brief=True))
    return result


def _write_fp_review(rows: list[dict[str, Any]], out_dir: Path) -> None:
    """``fp_runs.gpkg`` (WGS84 lines) + ``fp_runs.csv``, longest and highest-scoring first."""
    out_dir.mkdir(parents=True, exist_ok=True)
    gpkg, csv = out_dir / "fp_runs.gpkg", out_dir / "fp_runs.csv"
    gpkg.unlink(missing_ok=True)
    if not rows:
        csv.write_text("")
        return
    frames = []
    for crs in sorted({r["crs"] for r in rows}):
        sub = [{k: v for k, v in r.items() if k != "crs"} for r in rows if r["crs"] == crs]
        frames.append(gpd.GeoDataFrame(sub, geometry="geometry", crs=crs).to_crs("EPSG:4326"))
    gdf = gpd.pd.concat(frames, ignore_index=True)
    gdf["priority"] = gdf["length_m"] * gdf["mean_score"]
    gdf = gdf.sort_values("priority", ascending=False).reset_index(drop=True)
    gdf.insert(0, "run", gdf.index)
    gdf.to_file(gpkg, layer="fp_runs", driver="GPKG")
    gdf.drop(columns="geometry").to_csv(csv, index=False, float_format="%.4f")


def _canyon_balanced_weights(mrs: list[MosaicReaches]) -> list[np.ndarray]:
    """Training weights where every canyon's positive reaches sum to the same total
    (the mean canyon length), so one 17 km creek walk can't outweigh ten short
    canyons. Negatives keep their length weights. Metrics stay length-weighted."""

    def key(mr: MosaicReaches, i: int) -> str:
        return mr.canyon_keys[mr.canyon[i]] if mr.canyon[i] >= 0 else "?"

    totals: dict[str, float] = {}
    for mr in mrs:
        for i in np.flatnonzero(mr.label == 1):
            totals[key(mr, i)] = totals.get(key(mr, i), 0.0) + float(mr.length_m[i])
    mean_total = float(np.mean(list(totals.values()))) if totals else 1.0
    out = []
    for mr in mrs:
        w = mr.length_m.astype(np.float64).copy()
        for i in np.flatnonzero(mr.label == 1):
            w[i] *= mean_total / totals[key(mr, i)]
        out.append(w)
    return out


def _base_rate(m: dict[str, float]) -> float:
    total = m["pos_km"] + m["neg_km"]
    return m["pos_km"] / total if total else math.nan


def _technical_summary(r: dict[str, Any]) -> str:
    secs = r.get("technical_sections") or []
    if not secs:
        return "(off)"
    marked = [t for t in secs if t["marked"]]
    return (
        f"mode={r['technical']}, unmarked={r['unmarked']}: {len(marked)}/{len(secs)} "
        f"positive tracks marked; their sections cover "
        f"{sum(t['section_km'] for t in marked):.1f} of "
        f"{sum(t['full_km'] for t in marked):.1f} descent km. Unmarked tracks: "
        f"{sum(t['full_km'] for t in secs if not t['marked']):.1f} km.\n"
    )


def _labels_summary(r: dict[str, Any]) -> str:
    snap = r["config"].get("label_snap_m", 0)
    lines = [
        f"- matching: {'map-matched to the network, ' + f'{snap:g} m' if snap else 'pixel distance'}"
        f" (tol {r['config']['label_tol_m']:g} m)",
        f"- overrides: {r['label_overrides']['path'] or '(none)'}, "
        f"{r['label_overrides']['reaches_changed']} reaches changed",
        f"- train-only inputs (not scored): {len(r['train_only'])}",
        f"- ranker for outputs/threshold: `{r['smoothing']['ranker']}`; "
        f"{r['fp_runs']} negative runs above threshold in `review/fp_runs.*`",
    ]
    st = r.get("snap_stats") or []
    if st:
        off = np.array([s["median_offset_m"] for s in st], dtype=float)
        mf = np.array([s["matched_frac"] for s in st], dtype=float)
        lines.append(
            f"- snapped tracks: {len(st)}; matched point fraction median {np.median(mf):.2f} "
            f"(min {mf.min():.2f}); median snap offset median {np.nanmedian(off):.1f} m "
            f"(max {np.nanmax(off):.1f} m)"
        )
    return "\n".join(lines) + "\n"


def _report_md(r: dict[str, Any], brief: bool = False) -> str:
    metric_cols = ["auc", "ap", "r_at_p50", "p_at_r80"]
    am_cols = ["auc_am", "ap_am", "base_rate_am"]
    parts = [
        "# Reach evaluation\n",
        "Length-weighted over reaches labeled 0/1. Learned models are leave-one-group-out "
        "(overlapping mosaics/tiles held out together). `*_am` = area-matched: only reaches "
        f"with log_area >= {r['area_matched_log_area_min']:.2f} (10th pct of positives), so "
        "drainage size alone can't separate them; compare `ap_am` to `base_rate_am`.\n",
        "## Methods\n",
        _table(
            r["methods"],
            ["method", "mosaics", *metric_cols, *am_cols, "median_mosaic_ap", "pos_km", "neg_km"],
        ),
        f"\n## Per mosaic ({r['models'][0]}, leave-one-group-out)\n",
        _table(
            r["mosaics"],
            [
                "mosaic",
                "group",
                "reaches",
                "pos_kmls",
                "ignore_kmls",
                "ignore_pts",
                *metric_cols,
                "ap_am",
                "auc_log_area",
                "pos_km",
                "neg_km",
            ],
        ),
        "\n## Single features (pooled; AUC < 0.5 means smaller values rank higher)\n",
        _table(r["single_features"][: 15 if brief else None], ["feature", *metric_cols, *am_cols]),
        "\n## Technical sections\n",
        _technical_summary(r),
        "\n## Labels\n",
        _labels_summary(r),
        "\n## Rappel candidates (D8 profile drops on positive reaches)\n",
        "```\n" + json.dumps(r["rappels"]["summary"], indent=1) + "\n```",
    ]
    if r.get("rappels_iso"):
        parts += [
            "\n## Rappel candidates (isotonic-profile steps on positive reaches)\n",
            "```\n" + json.dumps(r["rappels_iso"]["summary"], indent=1) + "\n```",
        ]
    if not brief:
        coefs = sorted(r["coefficients_full_fit"].items(), key=lambda kv: -abs(kv[1]))
        parts += [
            "\n## Logistic regression coefficients (full fit, standardized features)\n",
            _table([{"feature": k, "coef": v} for k, v in coefs], ["feature", "coef"]),
            "\n## Per-canyon rappel steps\n",
            _table(
                r["rappels"]["canyons"],
                [
                    "mosaic",
                    "kml",
                    "track_km",
                    "ropewiki_rappels",
                    "n_steps@excess2",
                    "n_steps@excess4",
                    "n_steps@excess8",
                    "ropewiki_longest_m",
                    "max_drop_m",
                ],
            ),
            "\n## Label sources\n",
            _table(r["label_notes"], ["mosaic", "title", "action"])
            if r["label_notes"]
            else "(none)",
        ]
    return "\n".join(parts) + "\n"


def reach_eval_cli(argv: list[str] | None = None) -> None:
    d = ReachConfig()
    p = argparse.ArgumentParser(
        prog="canyongis-reach-eval",
        description=(
            "Build drainage reaches per manifest mosaic, label them from descent tracks, "
            "and score leave-one-group-out rankers, single features, and external "
            "rasters. Writes report.md, metrics.json, per-mosaic GPKG + PNG."
        ),
    )
    p.add_argument("manifest", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--root", type=Path, help="Base dir for manifest paths (default: manifest dir).")
    p.add_argument("--cache-dir", type=Path, help="Reach/Ropewiki cache (default: OUT/cache).")
    p.add_argument("--max-pixels", type=float, default=60e6, help="Tile (or skip) larger mosaics.")
    p.add_argument(
        "--tile-px",
        type=int,
        default=0,
        help="Core tile size for mosaics over --max-pixels (0 = skip them).",
    )
    p.add_argument("--tile-overlap-px", type=int, default=750)
    p.add_argument("--only", nargs="*", default=[], help="Only mosaics whose name contains any.")
    p.add_argument("--ropewiki", action="store_true", help="Refine labels with Ropewiki metadata.")
    p.add_argument("--nontechnical", choices=("ignore", "positive"), default="ignore")
    p.add_argument(
        "--min-rappels",
        type=int,
        default=0,
        help="With --ropewiki: ignore tracks whose Ropewiki rappel count is below this.",
    )
    p.add_argument(
        "--technical",
        choices=("off", "markers"),
        default="markers",
        help="markers: positives are only the descent stretch spanning named rappel points "
        "(R1, rap 2, ...) ± --technical-margin-m; the rest of the descent is ignored.",
    )
    p.add_argument(
        "--unmarked",
        choices=("whole", "ignore"),
        default="whole",
        help="With --technical markers: tracks without usable rappel points stay whole "
        "positives or are ignored entirely.",
    )
    p.add_argument("--technical-margin-m", type=float, default=200.0)
    p.add_argument(
        "--balance-canyons",
        action="store_true",
        help="Train with equal total weight per canyon (metrics stay length-weighted).",
    )
    p.add_argument(
        "--raster",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Score an external raster (PATH may contain {stem}). Repeatable.",
    )
    p.add_argument(
        "--models",
        default="logreg,gbt",
        help=f"Comma-separated models from {MODELS}; the first drives GPKG/PNG outputs.",
    )
    p.add_argument("--features", help="Comma-separated ranker features (default: built-in set).")
    p.add_argument(
        "--extra-train-manifest",
        type=Path,
        help="Manifest of extra mosaics used for training only (never scored).",
    )
    p.add_argument(
        "--exclude-track",
        action="append",
        default=[],
        metavar="TITLE",
        help="Ropewiki title of a track to ignore instead of labeling positive "
        "(e.g. 'Das Pfeife Canyon'). Repeatable.",
    )
    p.add_argument(
        "--label-overrides",
        type=Path,
        help="Vector file of reviewed labels: 'label' column (positive/negative/ignore), "
        "optional 'buffer_m'. Applied after all other label rules.",
    )
    p.add_argument(
        "--label-snap-m",
        type=float,
        default=d.label_snap_m,
        help="Map-match positive tracks onto the network within this distance "
        "(0 = label by pixel distance to the track).",
    )
    p.add_argument(
        "--smooth-hops",
        default="1",
        help="Comma-separated main-stem smoothing widths (reaches each way) to score; the "
        "first (with the first --smooth mode) drives outputs. Empty = no smoothing.",
    )
    p.add_argument("--smooth", default="median", help="Comma-separated: median, mean.")
    p.add_argument(
        "--profile",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compute isotonic-profile step features (cached separately; opt into the ranker "
        "with --features) and score the steps as rappel candidates.",
    )
    pd_ = ProfileConfig()
    p.add_argument("--step-grade", type=float, default=pd_.step_grade)
    p.add_argument("--step-window-m", type=float, default=pd_.step_window_m)
    p.add_argument("--min-step-m", type=float, default=pd_.min_step_m)
    p.add_argument("--corridor-px", type=int, default=pd_.corridor_px)
    p.add_argument("--no-png", action="store_true")
    p.add_argument("--min-area-m2", type=float, default=d.min_area_m2)
    p.add_argument("--reach-length-m", type=float, default=d.reach_length_m)
    p.add_argument("--label-tol-m", type=float, default=d.label_tol_m)
    p.add_argument("--label-min-frac", type=float, default=d.label_min_frac)
    p.add_argument("--ignore-radius-m", type=float, default=d.ignore_radius_m)
    p.add_argument("--drop-window-m", type=float, default=d.drop_window_m)
    p.add_argument("--min-drop-m", type=float, default=d.min_drop_m)
    p.add_argument("--context-hops", type=int, default=d.context_hops)
    a = p.parse_args(argv)

    rasters = {}
    for spec in a.raster:
        name, _, path = spec.partition("=")
        if not path:
            p.error(f"--raster expects NAME=PATH, got {spec!r}")
        rasters[name] = path
    cfg = ReachConfig(
        min_area_m2=a.min_area_m2,
        reach_length_m=a.reach_length_m,
        label_tol_m=a.label_tol_m,
        label_min_frac=a.label_min_frac,
        ignore_radius_m=a.ignore_radius_m,
        drop_window_m=a.drop_window_m,
        min_drop_m=a.min_drop_m,
        context_hops=a.context_hops,
        label_snap_m=a.label_snap_m,
    )
    profile = (
        ProfileConfig(
            corridor_px=a.corridor_px,
            step_window_m=a.step_window_m,
            step_grade=a.step_grade,
            min_step_m=a.min_step_m,
        )
        if a.profile
        else None
    )
    run_reach_eval(
        a.manifest,
        a.out_dir,
        root=a.root,
        cache_dir=a.cache_dir,
        cfg=cfg,
        max_pixels=a.max_pixels,
        tile_px=a.tile_px,
        tile_overlap_px=a.tile_overlap_px,
        only=a.only,
        use_ropewiki=a.ropewiki,
        nontechnical=a.nontechnical,
        min_rappels=a.min_rappels,
        balance_canyons=a.balance_canyons,
        technical=a.technical,
        unmarked=a.unmarked,
        technical_margin_m=a.technical_margin_m,
        rasters=rasters,
        models=tuple(a.models.split(",")),
        features=a.features.split(",") if a.features else None,
        write_png=not a.no_png,
        profile=profile,
        smooth_hops=tuple(int(h) for h in a.smooth_hops.split(",") if h),
        smooth_modes=tuple(m for m in a.smooth.split(",") if m),
        label_overrides=a.label_overrides,
        extra_train_manifest=a.extra_train_manifest,
        exclude_tracks=tuple(a.exclude_track),
    )


if __name__ == "__main__":
    reach_eval_cli()

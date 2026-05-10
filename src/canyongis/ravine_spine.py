"""Extract a canyon spine network from a multi-scale TPI score raster.

Pipeline
--------
1. Optional **grey closing on the score** to bridge momentary dips along a
   real canyon (bridges gaps without needing a looser global threshold).
2. **Hysteresis threshold** on the score: ``high`` (Otsu or fixed) seeds
   strong canyon-bottom pixels; ``low = high * hysteresis_ratio`` is kept
   only when 8-connected to a seed.
3. Optional binary-closing to bridge 1-pixel mask gaps (disabled by default;
   score closing should do the gap bridging for the v3 trunk workflow).
4. Drop connected components smaller than ``min_component_px``.
5. Skeletonize → 1-pixel medial axis.
6. **Trunk-and-major-branches per component**: each connected skeleton
   component contributes its diameter (longest path = trunk) plus any
   subtree whose own longest path is at least ``branch_keep_ratio`` of the
   parent's length, recursively. This replaces iterative spur pruning and
   handles "false short spurs" by construction.
7. Orient paths downhill and compute elevation-profile metrics: net drop,
   cumulative drop/gain, max step gain/drop, absolute grade, uphill fraction,
   and reversal count. Optional gain/uphill filters catch spur-crossing joins.
8. Drop paths whose total rise/run exceeds ``max_path_slope`` (kills wall
   gullies that masquerade as branches).
9. Snap each remaining path to the local elevation minimum inside the mask
   within a disk of radius ``snap_radius_m``.
10. Vectorize spines as LineStrings and sudden-drop candidates as Points
   → KML + GPKG.

The TPI score raster is intended to be the v6 ``b_tpi.tif`` produced by
:mod:`canyongis.ravine_compare` (multi-scale ``normalized_sum`` TPI).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import xy as rio_xy
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point
from skimage.filters import threshold_otsu
from skimage.morphology import skeletonize

from .io import log, read_dem, write_raster, write_vector


@dataclass(frozen=True)
class RavineSpineResult:
    out_dir: str
    threshold: float
    mask_px: int
    skeleton_px: int
    n_paths: int
    spines_path: str


@dataclass(frozen=True)
class ElevationProfile:
    """Elevation-change metrics sampled along a pixel path."""

    sample_count: int
    valid_fraction: float
    length_m: float
    min_elev_m: float | None
    max_elev_m: float | None
    relief_m: float | None
    net_drop_m: float | None
    total_drop_m: float | None
    total_gain_m: float | None
    max_step_drop_m: float | None
    max_step_gain_m: float | None
    max_step_grade: float | None
    mean_abs_grade: float | None
    uphill_fraction: float | None
    reversal_count: int


# -----------------------------------------------------------------------------
# Skeleton helpers
# -----------------------------------------------------------------------------


def _skeleton_to_paths(skel: np.ndarray) -> list[list[tuple[int, int]]]:
    """Walk a binary skeleton; return pixel paths split at branches/endpoints.

    Duplicated from :mod:`canyongis.ml.predict` so this module doesn't pull in
    torch as a transitive import.
    """
    ys, xs = np.where(skel)
    pixels = set(zip(ys.tolist(), xs.tolist(), strict=True))
    if not pixels:
        return []

    def nbrs(p: tuple[int, int]) -> list[tuple[int, int]]:
        y, x = p
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                q = (y + dy, x + dx)
                if q in pixels:
                    out.append(q)
        return out

    visited_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def edge_key(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start: tuple[int, int]) -> list[list[tuple[int, int]]]:
        results: list[list[tuple[int, int]]] = []
        for first in nbrs(start):
            if edge_key(start, first) in visited_edges:
                continue
            path = [start, first]
            visited_edges.add(edge_key(start, first))
            cur, prev = first, start
            while True:
                ns = [n for n in nbrs(cur) if n != prev and edge_key(cur, n) not in visited_edges]
                if len(ns) != 1 or len(nbrs(cur)) > 2:
                    break
                nxt = ns[0]
                visited_edges.add(edge_key(cur, nxt))
                path.append(nxt)
                prev, cur = cur, nxt
            if len(path) >= 2:
                results.append(path)
        return results

    paths: list[list[tuple[int, int]]] = []
    starts = [p for p in pixels if len(nbrs(p)) == 1 or len(nbrs(p)) >= 3]
    for start in starts:
        paths.extend(walk(start))
    for p in pixels:
        if all(edge_key(p, n) in visited_edges for n in nbrs(p)):
            continue
        paths.extend(walk(p))
    return paths


def _skeleton_degrees(skel: np.ndarray) -> dict[tuple[int, int], int]:
    """8-neighbor degree of every skeleton pixel."""
    pixels = {(int(y), int(x)) for y, x in np.argwhere(skel)}
    out: dict[tuple[int, int], int] = {}
    for p in pixels:
        y, x = p
        d = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if (dy or dx) and (y + dy, x + dx) in pixels:
                    d += 1
        out[p] = d
    return out


def _path_length_m(path: list[tuple[int, int]], pixel_size: float) -> float:
    """Geodesic length of a pixel path in meters (counts diagonals as √2)."""
    total = 0.0
    for (y0, x0), (y1, x1) in pairwise(path):
        total += float(np.hypot(y1 - y0, x1 - x0))
    return total * pixel_size


def _path_profile_arrays(
    path: list[tuple[int, int]], elev: np.ndarray, pixel_size: float
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]]]:
    """Return cumulative distance, elevation, and pixels for finite samples."""
    h, w = elev.shape
    distances: list[float] = []
    elevs: list[float] = []
    pixels: list[tuple[int, int]] = []
    dist = 0.0
    prev: tuple[int, int] | None = None
    for y, x in path:
        if prev is not None:
            dist += float(np.hypot(y - prev[0], x - prev[1])) * pixel_size
        prev = (y, x)
        if 0 <= y < h and 0 <= x < w and np.isfinite(elev[y, x]):
            distances.append(dist)
            elevs.append(float(elev[y, x]))
            pixels.append((y, x))
    return (
        np.asarray(distances, dtype=np.float64),
        np.asarray(elevs, dtype=np.float64),
        pixels,
    )


def _orient_path_downhill(
    path: list[tuple[int, int]], elev: np.ndarray, pixel_size: float
) -> list[tuple[int, int]]:
    """Orient a path from higher endpoint to lower endpoint when possible."""
    _dist, elevs, _pixels = _path_profile_arrays(path, elev, pixel_size)
    if elevs.size < 2:
        return path
    return list(reversed(path)) if elevs[0] < elevs[-1] else path


def _elevation_profile(
    path: list[tuple[int, int]], elev: np.ndarray, pixel_size: float
) -> ElevationProfile:
    """Summarize elevation change along a path oriented from high to low."""
    dist, elevs, _pixels = _path_profile_arrays(path, elev, pixel_size)
    length_m = _path_length_m(path, pixel_size)
    valid_fraction = float(len(elevs) / len(path)) if path else 0.0
    if elevs.size < 2:
        value = float(elevs[0]) if elevs.size == 1 else None
        return ElevationProfile(
            sample_count=int(elevs.size),
            valid_fraction=valid_fraction,
            length_m=length_m,
            min_elev_m=value,
            max_elev_m=value,
            relief_m=0.0 if value is not None else None,
            net_drop_m=None,
            total_drop_m=None,
            total_gain_m=None,
            max_step_drop_m=None,
            max_step_gain_m=None,
            max_step_grade=None,
            mean_abs_grade=None,
            uphill_fraction=None,
            reversal_count=0,
        )

    dz = np.diff(elevs)
    dd = np.diff(dist)
    valid_steps = dd > 1e-6
    step_grade = np.zeros_like(dz)
    step_grade[valid_steps] = dz[valid_steps] / dd[valid_steps]
    drops = np.maximum(-dz, 0.0)
    gains = np.maximum(dz, 0.0)
    step_signs = np.sign(dz[np.abs(dz) > 0.25])
    reversal_count = (
        int(np.count_nonzero(step_signs[1:] != step_signs[:-1])) if step_signs.size > 1 else 0
    )
    uphill_run = float(dd[(dz > 0.25) & valid_steps].sum())
    valid_run = float(dd[valid_steps].sum())
    return ElevationProfile(
        sample_count=int(elevs.size),
        valid_fraction=valid_fraction,
        length_m=length_m,
        min_elev_m=float(np.min(elevs)),
        max_elev_m=float(np.max(elevs)),
        relief_m=float(np.max(elevs) - np.min(elevs)),
        net_drop_m=float(elevs[0] - elevs[-1]),
        total_drop_m=float(drops.sum()),
        total_gain_m=float(gains.sum()),
        max_step_drop_m=float(drops.max()) if drops.size else None,
        max_step_gain_m=float(gains.max()) if gains.size else None,
        max_step_grade=float(np.max(np.abs(step_grade[valid_steps])))
        if valid_steps.any()
        else None,
        mean_abs_grade=float(np.average(np.abs(step_grade[valid_steps]), weights=dd[valid_steps]))
        if valid_steps.any()
        else None,
        uphill_fraction=uphill_run / valid_run if valid_run > 0 else None,
        reversal_count=reversal_count,
    )


def _path_slope(path: list[tuple[int, int]], elev: np.ndarray, pixel_size: float) -> float:
    """Rise/run of a path. Returns 0 if not enough finite elevations."""
    h, w = elev.shape
    elevs = [
        float(elev[y, x])
        for (y, x) in path
        if 0 <= y < h and 0 <= x < w and np.isfinite(elev[y, x])
    ]
    if len(elevs) < 2:
        return 0.0
    rise = max(elevs) - min(elevs)
    run = _path_length_m(path, pixel_size)
    return rise / max(run, 1e-6)


# -----------------------------------------------------------------------------
# Score gap-bridging
# -----------------------------------------------------------------------------


def close_score(score: np.ndarray, radius_px: int) -> np.ndarray:
    """Grayscale closing (max-then-min) on the score, NaN-safe.

    Lifts score dips smaller than the structuring element up toward the
    surrounding maximum, then restores the surface — the standard morphological
    'fill small valleys' operation. Used to bridge canyon-floor breaks where
    the TPI score momentarily dips below the hysteresis low band.
    """
    if radius_px <= 0:
        return score
    finite = np.isfinite(score)
    if not finite.any():
        return score.copy()
    low = float(np.nanmin(score)) - 1.0
    filled = np.where(finite, score, low).astype(np.float32)
    size = 2 * radius_px + 1
    closed = ndi.grey_closing(filled, size=(size, size), mode="nearest")
    out = closed.astype(np.float32)
    out[~finite] = np.nan
    return out


# -----------------------------------------------------------------------------
# Trunk-and-major-branches extraction (replaces iterative spur pruning)
# -----------------------------------------------------------------------------


def _build_skeleton_adj(
    skel: np.ndarray, pixel_size: float
) -> dict[tuple[int, int], list[tuple[tuple[int, int], float]]]:
    """8-connected adjacency: pixel -> list of (neighbor, edge_len_m).

    Diagonal edges are kept only for true diagonal runs. When an orthogonal
    corner pixel is present, the diagonal would be a shortcut across a 2x2
    junction block and can make shortest-path reconstruction jump branches.
    """
    pixels = {(int(y), int(x)) for y, x in np.argwhere(skel)}
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], float]]] = {}
    for p in pixels:
        y, x = p
        nbrs: list[tuple[tuple[int, int], float]] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                q = (y + dy, x + dx)
                if q not in pixels:
                    continue
                if (
                    abs(dy) == 1
                    and abs(dx) == 1
                    and ((y, x + dx) in pixels or (y + dy, x) in pixels)
                ):
                    continue
                nbrs.append((q, float(np.hypot(dy, dx)) * pixel_size))
        adj[p] = nbrs
    return adj


def _connected_components_adj(
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], float]]],
    nodes: set[tuple[int, int]] | None = None,
) -> list[set[tuple[int, int]]]:
    """Connected components over ``nodes`` (default: all of adj)."""
    universe = set(adj.keys()) if nodes is None else (nodes & set(adj.keys()))
    seen: set[tuple[int, int]] = set()
    out: list[set[tuple[int, int]]] = []
    for start in universe:
        if start in seen:
            continue
        stack = [start]
        comp: set[tuple[int, int]] = set()
        while stack:
            u = stack.pop()
            if u in seen or u not in universe:
                continue
            seen.add(u)
            comp.add(u)
            for v, _ in adj.get(u, ()):
                if v not in seen and v in universe:
                    stack.append(v)
        if comp:
            out.append(comp)
    return out


def _dijkstra_farthest(
    start: tuple[int, int],
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], float]]],
    allowed: set[tuple[int, int]] | None = None,
) -> tuple[tuple[int, int], dict[tuple[int, int], float], dict]:
    """Dijkstra restricted to ``allowed`` (default: all of adj).

    Returns (farthest_node, dist_dict, prev_dict).
    """
    import heapq

    dist: dict[tuple[int, int], float] = {start: 0.0}
    prev: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, w in adj.get(u, ()):
            if allowed is not None and v not in allowed:
                continue
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    far = max(dist, key=dist.get) if dist else start
    return far, dist, prev


def _reconstruct_path(prev: dict, end: tuple[int, int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = end
    while cur is not None:
        out.append(cur)
        cur = prev.get(cur)
    return list(reversed(out))


def _longest_path(
    adj: dict, allowed: set[tuple[int, int]], start: tuple[int, int] | None = None
) -> tuple[list[tuple[int, int]], float]:
    """Diameter (longest shortest-path) of the subgraph restricted to ``allowed``.

    If ``start`` is given, runs a single Dijkstra from there and returns the
    longest path *starting at* ``start`` — used for branches anchored to a
    parent path.
    """
    if not allowed:
        return [], 0.0
    if start is not None:
        far, dist, prev = _dijkstra_farthest(start, adj, allowed)
        return _reconstruct_path(prev, far), float(dist[far])
    seed = next(iter(allowed))
    a, _, _ = _dijkstra_farthest(seed, adj, allowed)
    b, dist, prev = _dijkstra_farthest(a, adj, allowed)
    return _reconstruct_path(prev, b), float(dist[b])


def _extract_branches_recursive(
    adj: dict,
    component: set[tuple[int, int]],
    parent_path_set: set[tuple[int, int]],
    parent_len: float,
    branch_keep_ratio: float,
    max_depth: int = 6,
) -> list[list[tuple[int, int]]]:
    """Recursively extract subtrees off ``parent_path_set``.

    Each subtree's longest path is kept iff its length is at least
    ``branch_keep_ratio * parent_len``. The kept branch is prepended with its
    parent-attachment pixel so the line visually joins the parent path.
    """
    if max_depth <= 0:
        return []
    off = component - parent_path_set
    if not off:
        return []
    out: list[list[tuple[int, int]]] = []
    sub_components = _connected_components_adj(adj, off)
    for sub in sub_components:
        # In an ideal tree there is one parent attachment. Skeleton junction
        # clusters can expose several; choose the attachment that produces the
        # longest anchored branch, deterministically.
        anchors: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for p in sorted(sub):
            for q, _ in sorted(adj.get(p, ())):
                if q in parent_path_set:
                    anchors.append((q, p))
        if not anchors:
            continue

        best: tuple[float, tuple[int, int], tuple[int, int], list[tuple[int, int]]] | None = None
        for anchor_parent, anchor_sub in anchors:
            sub_path, sub_len = _longest_path(adj, sub, start=anchor_sub)
            candidate = (sub_len, anchor_parent, anchor_sub, sub_path)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
        if best is None:
            continue
        sub_len, anchor_parent, _anchor_sub, sub_path = best
        if not sub_path or sub_len < branch_keep_ratio * parent_len:
            continue
        out.append([anchor_parent, *sub_path])
        out.extend(
            _extract_branches_recursive(
                adj,
                sub,
                set(sub_path),
                sub_len,
                branch_keep_ratio,
                max_depth - 1,
            )
        )
    return out


def extract_trunk_and_branches(
    skel: np.ndarray,
    pixel_size: float,
    *,
    branch_keep_ratio: float = 0.20,
    min_trunk_length_m: float = 0.0,
) -> list[list[tuple[int, int]]]:
    """Per connected skeleton component: trunk = longest path; recursively keep
    branches whose longest path >= ``branch_keep_ratio x parent_len``.

    Returns a list of pixel paths (trunk first per component, then branches).
    """
    adj = _build_skeleton_adj(skel, pixel_size)
    if not adj:
        return []
    out: list[list[tuple[int, int]]] = []
    for comp in _connected_components_adj(adj):
        if len(comp) < 2:
            continue
        trunk, trunk_len = _longest_path(adj, comp)
        if not trunk or trunk_len < min_trunk_length_m:
            continue
        out.append(trunk)
        if branch_keep_ratio > 0:
            out.extend(
                _extract_branches_recursive(
                    adj,
                    comp,
                    set(trunk),
                    trunk_len,
                    branch_keep_ratio,
                )
            )
    return out


# -----------------------------------------------------------------------------
# Snap to elevation minimum
# -----------------------------------------------------------------------------


def snap_path_to_elev_min(
    path: list[tuple[int, int]],
    dem: np.ndarray,
    mask: np.ndarray,
    radius_px: int,
) -> list[tuple[int, int]]:
    """Move each pixel to the lowest in-mask elevation within a square window.

    The window is a (2r+1) box around the original pixel, intersected with the
    drainage mask so the snap stays inside the same drainage. Consecutive
    duplicates (created when adjacent pixels snap to the same minimum) are
    collapsed.
    """
    h, w = dem.shape
    out: list[tuple[int, int]] = []
    for y, x in path:
        y0 = max(0, y - radius_px)
        y1 = min(h, y + radius_px + 1)
        x0 = max(0, x - radius_px)
        x1 = min(w, x + radius_px + 1)
        local_dem = dem[y0:y1, x0:x1].astype(np.float32, copy=True)
        local_mask = mask[y0:y1, x0:x1]
        local_dem[~local_mask] = np.inf
        local_dem[~np.isfinite(local_dem)] = np.inf
        if not np.isfinite(local_dem).any():
            out.append((y, x))
            continue
        idx = np.unravel_index(int(np.argmin(local_dem)), local_dem.shape)
        out.append((y0 + int(idx[0]), x0 + int(idx[1])))
    deduped: list[tuple[int, int]] = [out[0]]
    for p in out[1:]:
        if p != deduped[-1]:
            deduped.append(p)
    return deduped


# -----------------------------------------------------------------------------
# Vectorize
# -----------------------------------------------------------------------------


def _profile_row(profile: ElevationProfile) -> dict[str, float | int | None]:
    return {
        "profile_samples": profile.sample_count,
        "profile_valid_frac": profile.valid_fraction,
        "min_elev_m": profile.min_elev_m,
        "max_elev_m": profile.max_elev_m,
        "drop_m": profile.relief_m,
        "net_drop_m": profile.net_drop_m,
        "total_drop_m": profile.total_drop_m,
        "total_gain_m": profile.total_gain_m,
        "max_step_drop_m": profile.max_step_drop_m,
        "max_step_gain_m": profile.max_step_gain_m,
        "max_step_grade": profile.max_step_grade,
        "mean_abs_grade": profile.mean_abs_grade,
        "uphill_fraction": profile.uphill_fraction,
        "elev_reversals": profile.reversal_count,
    }


def _paths_to_gdf(
    paths_pix: list[list[tuple[int, int]]],
    transform,
    crs,
    elev: np.ndarray,
    score: np.ndarray,
    *,
    pixel_size: float,
    simplify_m: float,
    min_length_m: float,
) -> gpd.GeoDataFrame:
    cols = [
        "path_id",
        "length_m",
        "min_elev_m",
        "max_elev_m",
        "drop_m",
        "net_drop_m",
        "total_drop_m",
        "total_gain_m",
        "max_step_drop_m",
        "max_step_gain_m",
        "max_step_grade",
        "mean_abs_grade",
        "uphill_fraction",
        "elev_reversals",
        "mean_score",
        "geometry",
    ]
    rows = []
    h, w = score.shape
    for path_id, path in enumerate(paths_pix, start=1):
        if len(path) < 2:
            continue
        coords = [rio_xy(transform, int(y), int(x)) for (y, x) in path]
        line = LineString(coords)
        if simplify_m > 0:
            line = line.simplify(simplify_m, preserve_topology=False)
        if line.length < min_length_m or line.is_empty:
            continue
        profile = _elevation_profile(path, elev, pixel_size)
        scores = [
            float(score[y, x])
            for (y, x) in path
            if 0 <= y < h and 0 <= x < w and np.isfinite(score[y, x])
        ]
        rows.append(
            {
                "path_id": path_id,
                "length_m": float(line.length),
                **_profile_row(profile),
                "mean_score": float(np.mean(scores)) if scores else None,
                "geometry": line,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(columns=cols, geometry="geometry", crs=crs)
    return gpd.GeoDataFrame(rows, crs=crs)


def _path_rappel_candidates(
    path_id: int,
    path: list[tuple[int, int]],
    elev: np.ndarray,
    pixel_size: float,
    *,
    window_m: float,
    min_drop_m: float,
    min_grade: float,
    min_spacing_m: float,
) -> list[dict[str, object]]:
    """Detect abrupt downhill windows along a path as likely rappel locations."""
    dist, elevs, pixels = _path_profile_arrays(path, elev, pixel_size)
    if elevs.size < 2:
        return []
    candidates: list[dict[str, object]] = []
    for i in range(elevs.size - 1):
        max_j = int(np.searchsorted(dist, dist[i] + max(window_m, 1e-6), side="right"))
        if max_j <= i + 1:
            max_j = i + 2
        max_j = min(max_j, elevs.size)
        if max_j <= i + 1:
            continue
        downstream = elevs[i + 1 : max_j]
        drops = elevs[i] - downstream
        best_offset = int(np.argmax(drops))
        j = i + 1 + best_offset
        step_drops = -(np.diff(elevs[i : j + 1]))
        lip_idx = i + int(np.argmax(step_drops))
        best_drop = float(elevs[lip_idx] - elevs[j])
        run_m = float(dist[j] - dist[lip_idx])
        grade = best_drop / max(run_m, 1e-6)
        if best_drop >= min_drop_m and grade >= min_grade:
            candidates.append(
                {
                    "path_id": path_id,
                    "start_idx": lip_idx,
                    "end_idx": j,
                    "dist_m": float(dist[lip_idx]),
                    "drop_m": best_drop,
                    "run_m": run_m,
                    "grade": float(grade),
                    "elev_top_m": float(elevs[lip_idx]),
                    "elev_bottom_m": float(elevs[j]),
                    "pixel": pixels[lip_idx],
                }
            )
    if not candidates:
        return []

    kept: list[dict[str, object]] = []
    for cand in sorted(
        candidates,
        key=lambda item: (-float(item["grade"]), -float(item["drop_m"]), float(item["dist_m"])),
    ):
        if all(
            abs(float(cand["dist_m"]) - float(prev["dist_m"])) >= min_spacing_m for prev in kept
        ):
            kept.append(cand)
    return sorted(kept, key=lambda item: float(item["dist_m"]))


def _rappel_candidates_to_gdf(
    paths_pix: list[list[tuple[int, int]]],
    transform,
    crs,
    elev: np.ndarray,
    pixel_size: float,
    *,
    window_m: float,
    min_drop_m: float,
    min_grade: float,
    min_spacing_m: float,
) -> gpd.GeoDataFrame:
    cols = [
        "path_id",
        "drop_m",
        "run_m",
        "grade",
        "elev_top_m",
        "elev_bottom_m",
        "dist_m",
        "geometry",
    ]
    rows = []
    for path_id, path in enumerate(paths_pix, start=1):
        for cand in _path_rappel_candidates(
            path_id,
            path,
            elev,
            pixel_size,
            window_m=window_m,
            min_drop_m=min_drop_m,
            min_grade=min_grade,
            min_spacing_m=min_spacing_m,
        ):
            y, x = cand.pop("pixel")
            lon, lat = rio_xy(transform, int(y), int(x))
            rows.append({**cand, "geometry": Point(lon, lat)})
    if not rows:
        return gpd.GeoDataFrame(columns=cols, geometry="geometry", crs=crs)
    return gpd.GeoDataFrame(rows, crs=crs)


def _profile_summary(profiles: list[ElevationProfile]) -> dict[str, float | int]:
    def metric(name: str) -> np.ndarray:
        vals = [getattr(profile, name) for profile in profiles]
        return np.asarray([float(v) for v in vals if v is not None and np.isfinite(v)])

    summary: dict[str, float | int] = {"n_profiles": len(profiles)}
    for name in (
        "net_drop_m",
        "total_drop_m",
        "total_gain_m",
        "max_step_drop_m",
        "max_step_gain_m",
        "max_step_grade",
        "mean_abs_grade",
        "uphill_fraction",
    ):
        vals = metric(name)
        if vals.size:
            summary[f"{name}_p50"] = float(np.percentile(vals, 50))
            summary[f"{name}_p90"] = float(np.percentile(vals, 90))
            summary[f"{name}_max"] = float(np.max(vals))
    reversals = np.asarray([profile.reversal_count for profile in profiles], dtype=np.float64)
    if reversals.size:
        summary["elev_reversals_p90"] = float(np.percentile(reversals, 90))
        summary["elev_reversals_max"] = int(np.max(reversals))
    return summary


# -----------------------------------------------------------------------------
# Top-level
# -----------------------------------------------------------------------------


def _build_drainage_mask(
    score: np.ndarray,
    *,
    auto_threshold: str = "otsu",
    threshold_pct: float = 90.0,
    hysteresis_ratio: float = 0.6,
) -> tuple[np.ndarray, float, float]:
    """Return (mask, high, low) using hysteresis thresholding.

    Hysteresis: pixels with ``score >= high`` are seed canyon-bottom pixels;
    pixels with ``score >= low`` are kept only if 8-connected to a seed.
    Bridges momentary score dips along a real canyon without re-introducing
    isolated low-score noise.
    """
    finite = score[np.isfinite(score)]
    if finite.size == 0:
        raise ValueError("score raster is all-nan")
    if auto_threshold == "otsu":
        high = float(threshold_otsu(finite))
    elif auto_threshold == "fixed":
        high = float(np.percentile(finite, threshold_pct))
    else:
        raise ValueError(f"unknown auto_threshold mode: {auto_threshold!r}")
    low = high * hysteresis_ratio
    seeds = (score >= high) & np.isfinite(score)
    weak = (score >= low) & np.isfinite(score)
    structure = np.ones((3, 3), dtype=bool)
    mask = ndi.binary_propagation(seeds, structure=structure, mask=weak)
    return mask, high, low


def export_ravine_spines(
    score_path: str | Path,
    dem_path: str | Path,
    out_dir: str | Path,
    *,
    score_closing_m: float = 5.0,
    auto_threshold: str = "otsu",
    threshold_pct: float = 90.0,
    hysteresis_ratio: float = 0.6,
    closing_iters: int = 0,
    min_component_px: int = 50,
    branch_keep_ratio: float = 0.20,
    min_trunk_length_m: float = 0.0,
    max_path_slope: float | None = 0.30,
    max_total_gain_m: float | None = None,
    max_step_gain_m: float | None = None,
    max_uphill_fraction: float | None = None,
    snap_radius_m: float = 5.0,
    simplify_m: float = 2.0,
    min_line_length_m: float = 20.0,
    rappel_window_m: float = 10.0,
    rappel_min_drop_m: float = 5.0,
    rappel_min_grade: float = 0.5,
    rappel_min_spacing_m: float = 10.0,
) -> RavineSpineResult:
    score_path = Path(score_path)
    dem_path = Path(dem_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"ravine-spine: score={score_path} dem={dem_path} → {out_dir}")

    elev, transform, crs, pixel_size = read_dem(str(dem_path))
    with rasterio.open(score_path) as src:
        score = src.read(1).astype(np.float32)
        if src.nodata is not None:
            score[score == src.nodata] = np.nan
        score_transform = src.transform
        score_shape = (src.height, src.width)
    if score_shape != elev.shape or score_transform != transform:
        raise ValueError(
            f"score raster ({score_shape}, {score_transform}) does not align with DEM "
            f"({elev.shape}, {transform})"
        )

    if score_closing_m > 0:
        radius_px = max(round(score_closing_m / max(pixel_size, 1e-6)), 1)
        score = close_score(score, radius_px)
        log(f"score grey-closing radius={radius_px}px ({score_closing_m}m)")

    mask, high, thresh_low = _build_drainage_mask(
        score,
        auto_threshold=auto_threshold,
        threshold_pct=threshold_pct,
        hysteresis_ratio=hysteresis_ratio,
    )
    log(
        f"hysteresis ({auto_threshold}) high={high:.3f} low={thresh_low:.3f} "
        f"→ {int(mask.sum())} mask px"
    )

    if closing_iters > 0:
        mask = ndi.binary_closing(mask, iterations=closing_iters)
        log(f"after closing(iters={closing_iters}): {int(mask.sum())} mask px")

    if min_component_px > 0:
        labeled, n = ndi.label(mask)
        if n > 0:
            counts = np.bincount(labeled.ravel())
            keep = counts >= min_component_px
            keep[0] = False
            mask = keep[labeled]
            log(
                f"component filter (>= {min_component_px}px): {int(mask.sum())} mask px, "
                f"{int(keep.sum())}/{n} components kept"
            )

    skel = skeletonize(mask)
    log(f"skeleton: {int(skel.sum())} px")

    paths = extract_trunk_and_branches(
        skel,
        pixel_size,
        branch_keep_ratio=branch_keep_ratio,
        min_trunk_length_m=min_trunk_length_m,
    )
    log(
        f"trunk+branches (ratio={branch_keep_ratio}, min_trunk={min_trunk_length_m}m): "
        f"{len(paths)} paths"
    )

    paths = [_orient_path_downhill(p, elev, pixel_size) for p in paths]

    if max_path_slope is not None:
        before = len(paths)
        paths = [p for p in paths if _path_slope(p, elev, pixel_size) <= max_path_slope]
        log(f"after slope filter (<= {max_path_slope}): {before} → {len(paths)} paths")

    if max_total_gain_m is not None:
        before = len(paths)
        paths = [
            p
            for p in paths
            if (_elevation_profile(p, elev, pixel_size).total_gain_m or 0.0) <= max_total_gain_m
        ]
        log(f"after total-gain filter (<= {max_total_gain_m}m): {before} → {len(paths)} paths")

    if max_step_gain_m is not None:
        before = len(paths)
        paths = [
            p
            for p in paths
            if (_elevation_profile(p, elev, pixel_size).max_step_gain_m or 0.0) <= max_step_gain_m
        ]
        log(f"after step-gain filter (<= {max_step_gain_m}m): {before} → {len(paths)} paths")

    if max_uphill_fraction is not None:
        before = len(paths)
        paths = [
            p
            for p in paths
            if (_elevation_profile(p, elev, pixel_size).uphill_fraction or 0.0)
            <= max_uphill_fraction
        ]
        log(
            f"after uphill-fraction filter (<= {max_uphill_fraction}): "
            f"{before} → {len(paths)} paths"
        )

    snap_r_px = max(round(snap_radius_m / max(pixel_size, 1e-6)), 1)
    snapped = [snap_path_to_elev_min(p, elev, mask, snap_r_px) for p in paths]
    profiles = [_elevation_profile(p, elev, pixel_size) for p in snapped]

    # Re-render the kept skeleton from the kept paths (post-extraction, pre-snap)
    kept_skel = np.zeros_like(skel)
    for p in paths:
        for y, x in p:
            if 0 <= y < kept_skel.shape[0] and 0 <= x < kept_skel.shape[1]:
                kept_skel[y, x] = True
    skel = kept_skel

    gdf = _paths_to_gdf(
        snapped,
        transform,
        crs,
        elev,
        score,
        pixel_size=pixel_size,
        simplify_m=simplify_m,
        min_length_m=min_line_length_m,
    )
    log(f"{len(gdf)} spine LineStrings after length>={min_line_length_m}m filter")

    rappel_gdf = _rappel_candidates_to_gdf(
        snapped,
        transform,
        crs,
        elev,
        pixel_size,
        window_m=rappel_window_m,
        min_drop_m=rappel_min_drop_m,
        min_grade=rappel_min_grade,
        min_spacing_m=rappel_min_spacing_m,
    )
    log(
        f"{len(rappel_gdf)} rappel candidates "
        f"(drop>={rappel_min_drop_m}m grade>={rappel_min_grade} over {rappel_window_m}m)"
    )

    write_raster(
        str(out_dir / "drainage_mask.tif"),
        mask.astype(np.uint8),
        transform,
        crs,
        dtype="uint8",
    )
    write_raster(
        str(out_dir / "skeleton.tif"),
        skel.astype(np.uint8),
        transform,
        crs,
        dtype="uint8",
    )
    write_vector(gdf, out_dir / "spines")
    write_vector(rappel_gdf, out_dir / "rappel_candidates")

    summary = {
        "score_path": str(score_path),
        "dem_path": str(dem_path),
        "out_dir": str(out_dir),
        "pixel_size_m": pixel_size,
        "params": {
            "score_closing_m": score_closing_m,
            "auto_threshold": auto_threshold,
            "threshold_pct": threshold_pct,
            "hysteresis_ratio": hysteresis_ratio,
            "closing_iters": closing_iters,
            "min_component_px": min_component_px,
            "branch_keep_ratio": branch_keep_ratio,
            "min_trunk_length_m": min_trunk_length_m,
            "max_path_slope": max_path_slope,
            "max_total_gain_m": max_total_gain_m,
            "max_step_gain_m": max_step_gain_m,
            "max_uphill_fraction": max_uphill_fraction,
            "snap_radius_m": snap_radius_m,
            "simplify_m": simplify_m,
            "min_line_length_m": min_line_length_m,
            "rappel_window_m": rappel_window_m,
            "rappel_min_drop_m": rappel_min_drop_m,
            "rappel_min_grade": rappel_min_grade,
            "rappel_min_spacing_m": rappel_min_spacing_m,
        },
        "stats": {
            "threshold_high": high,
            "threshold_low": thresh_low,
            "mask_px": int(mask.sum()),
            "skeleton_px": int(skel.sum()),
            "n_paths": len(gdf),
            "n_rappel_candidates": len(rappel_gdf),
            "total_length_m": float(gdf.length.sum()) if not gdf.empty else 0.0,
            "profiles": _profile_summary(profiles),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    result = RavineSpineResult(
        out_dir=str(out_dir),
        threshold=high,
        mask_px=int(mask.sum()),
        skeleton_px=int(skel.sum()),
        n_paths=len(gdf),
        spines_path=str(out_dir / "spines.kml"),
    )
    log(f"wrote {result.spines_path}")
    return result


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def ravine_spine_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ravine-spine",
        description=(
            "Extract a canyon spine network from a multi-scale TPI score raster: "
            "threshold → component-clean → skeletonize → spur-prune → snap-to-thalweg → KML."
        ),
    )
    p.add_argument("score", type=Path, help="TPI score raster (e.g. v6 b_tpi.tif).")
    p.add_argument("dem", type=Path, help="Source DEM (must align with the score raster).")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument(
        "--score-closing-m",
        type=float,
        default=5.0,
        help=(
            "Grey-closing radius (m) on the score before thresholding. Bridges momentary "
            "score dips along real canyons. 0 disables (default: 5)."
        ),
    )
    p.add_argument(
        "--auto-threshold",
        choices=("otsu", "fixed"),
        default="otsu",
        help=(
            "How to pick the hysteresis 'high' threshold (default: otsu). "
            "'fixed' uses --threshold-pct."
        ),
    )
    p.add_argument(
        "--threshold-pct",
        type=float,
        default=90.0,
        help="Score percentile for 'high' threshold when --auto-threshold=fixed (default: 90).",
    )
    p.add_argument(
        "--hysteresis-ratio",
        type=float,
        default=0.6,
        help="low = high * ratio for hysteresis (default: 0.6).",
    )
    p.add_argument(
        "--branch-keep-ratio",
        type=float,
        default=0.20,
        help=(
            "Keep a sub-branch only if its longest path >= ratio * parent path length "
            "(default: 0.20). Set to 0 to keep only trunks."
        ),
    )
    p.add_argument(
        "--min-trunk-length-m",
        type=float,
        default=0.0,
        help="Drop entire components whose trunk is shorter than this (m) (default: 0).",
    )
    p.add_argument(
        "--max-path-slope",
        type=float,
        default=0.30,
        help=(
            "Drop paths whose rise/run exceeds this (kills wall gullies). Negative to "
            "disable (default: 0.30)."
        ),
    )
    p.add_argument(
        "--max-total-gain-m",
        type=float,
        default=-1.0,
        help=(
            "Drop paths with more cumulative uphill movement than this after downhill "
            "orientation. Negative disables (default: disabled)."
        ),
    )
    p.add_argument(
        "--max-step-gain-m",
        type=float,
        default=-1.0,
        help=(
            "Drop paths with any single uphill jump above this many meters. Useful for "
            "spur-crossing joins. Negative disables (default: disabled)."
        ),
    )
    p.add_argument(
        "--max-uphill-fraction",
        type=float,
        default=-1.0,
        help=(
            "Drop paths whose uphill run fraction exceeds this value after downhill "
            "orientation. Negative disables (default: disabled)."
        ),
    )
    p.add_argument(
        "--closing-iters",
        type=int,
        default=0,
        help="Binary-closing iterations on the mask (default: 0).",
    )
    p.add_argument(
        "--min-component-px",
        type=int,
        default=50,
        help="Drop drainage components smaller than this in pixels (default: 50).",
    )
    p.add_argument(
        "--snap-radius-m",
        type=float,
        default=5.0,
        help="Window radius (m) for elevation-minimum snap (default: 5).",
    )
    p.add_argument(
        "--simplify-m",
        type=float,
        default=2.0,
        help="Douglas-Peucker tolerance (m) on the snapped polylines (default: 2).",
    )
    p.add_argument(
        "--min-line-length-m",
        type=float,
        default=20.0,
        help="Drop output polylines shorter than this (m) (default: 20).",
    )
    p.add_argument(
        "--rappel-window-m",
        type=float,
        default=10.0,
        help="Along-path window for sudden-drop/rappel candidate analysis (default: 10).",
    )
    p.add_argument(
        "--rappel-min-drop-m",
        type=float,
        default=5.0,
        help="Minimum downhill change for a rappel candidate (default: 5).",
    )
    p.add_argument(
        "--rappel-min-grade",
        type=float,
        default=0.5,
        help="Minimum drop/run grade for a rappel candidate (default: 0.5).",
    )
    p.add_argument(
        "--rappel-min-spacing-m",
        type=float,
        default=10.0,
        help="Minimum spacing between rappel candidates on the same path (default: 10).",
    )
    args = p.parse_args(argv)

    max_slope = args.max_path_slope if args.max_path_slope >= 0 else None
    max_total_gain = args.max_total_gain_m if args.max_total_gain_m >= 0 else None
    max_step_gain = args.max_step_gain_m if args.max_step_gain_m >= 0 else None
    max_uphill_fraction = args.max_uphill_fraction if args.max_uphill_fraction >= 0 else None
    result = export_ravine_spines(
        args.score,
        args.dem,
        args.outdir,
        score_closing_m=args.score_closing_m,
        auto_threshold=args.auto_threshold,
        threshold_pct=args.threshold_pct,
        hysteresis_ratio=args.hysteresis_ratio,
        closing_iters=args.closing_iters,
        min_component_px=args.min_component_px,
        branch_keep_ratio=args.branch_keep_ratio,
        min_trunk_length_m=args.min_trunk_length_m,
        max_path_slope=max_slope,
        max_total_gain_m=max_total_gain,
        max_step_gain_m=max_step_gain,
        max_uphill_fraction=max_uphill_fraction,
        snap_radius_m=args.snap_radius_m,
        simplify_m=args.simplify_m,
        min_line_length_m=args.min_line_length_m,
        rappel_window_m=args.rappel_window_m,
        rappel_min_drop_m=args.rappel_min_drop_m,
        rappel_min_grade=args.rappel_min_grade,
        rappel_min_spacing_m=args.rappel_min_spacing_m,
    )
    print(json.dumps(asdict(result), indent=2))

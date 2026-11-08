"""Drainage-reach network with per-reach geomorphic features and track labels.

Candidate generation is hydrological: ~90% of known descent-track length lies within
15 m of the D8 network at A >= 1 ha (DETECTION_STRATEGY.md §3), so canyon detection
reduces to *ranking network reaches*. This module:

1. Traces the D8 stream network (A >= ``min_area_m2``) into links (head/junction →
   next junction/outlet) and splits each link into ~``reach_length_m`` reaches.
2. Computes cheap per-reach features: catchment, multi-scale confinement (-TPI),
   cross-section wall heights and valley widths, gradient, and profile steps.
3. Smooths a subset along the main stem (canyons are continuous) as ``ctx_*``.
4. Labels reaches from descent tracks: 1 = on a route, 0 = negative, -1 = ignore
   (partial overlap, route-end neighbors, near known-but-unmapped canyons).

``canyongis.reach_eval`` ranks and scores the cached result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.transform import xy as rio_xy
from rasterio.windows import Window
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point

from .hydrology import D8_OFFSETS, compute_flow_dir_accum
from .io import log, read_dem
from .ml.labels import _rasterize_lines
from .terrain import compute_tpi, fill_nans
from .validate import parse_kml

CACHE_VERSION = 2
LABEL_FIELDS = frozenset({"label_tol_m", "label_min_frac", "ignore_radius_m"})

# Features smoothed along the main stem into ``ctx_<name>``.
CONTEXT_FEATURES: tuple[str, ...] = (
    "depth150",
    "wall25",
    "wall50",
    "width5",
    "width20",
    "gradient",
    "max_drop",
    "max_step_excess",
    "drops_per_km",
)


@dataclass(frozen=True)
class ReachConfig:
    # Network
    min_area_m2: float = 10_000.0
    reach_length_m: float = 50.0
    fill_depressions: bool = True

    # Confinement (-TPI) radii
    depth_radii_m: tuple[float, ...] = (50.0, 150.0, 400.0)

    # Cross sections perpendicular to the thalweg
    xs_spacing_m: float = 5.0
    xs_max_m: float = 100.0
    xs_wall_m: tuple[float, ...] = (25.0, 50.0, 100.0)
    xs_width_rise_m: tuple[float, ...] = (5.0, 20.0)
    xs_dir_half_window_m: float = 5.0

    # Profile steps (rappel candidates)
    drop_window_m: float = 10.0
    min_drop_m: float = 3.0
    grade_window_m: float = 100.0

    # Main-stem smoothing: this many reaches up- and downstream
    context_hops: int = 3

    # Labels
    label_tol_m: float = 20.0
    label_min_frac: float = 0.5
    ignore_radius_m: float = 300.0

    def signature(self) -> str:
        """Hash of the fields that affect the cached network/features (not labels)."""
        fields = {k: v for k, v in asdict(self).items() if k not in LABEL_FIELDS}
        payload = json.dumps({"v": CACHE_VERSION, **fields}, sort_keys=True)
        return hashlib.sha1(payload.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class TileSpec:
    """A window of a large DEM. Reaches whose midpoint falls outside the core
    (window minus overlap margin) are marked ``core=False`` and ignored in labels."""

    row_off: int
    col_off: int
    height: int
    width: int
    core_rows: tuple[int, int]  # [start, stop) in tile coordinates
    core_cols: tuple[int, int]

    @property
    def window(self) -> Window:
        return Window(self.col_off, self.row_off, self.width, self.height)

    @property
    def suffix(self) -> str:
        return f"__r{self.row_off}_c{self.col_off}"


def plan_tiles(height: int, width: int, tile_px: int, overlap_px: int) -> list[TileSpec]:
    """Cover a raster with cores of ``tile_px`` plus ``overlap_px`` context on each side."""
    tiles = []
    for r0 in range(0, height, tile_px):
        for c0 in range(0, width, tile_px):
            ra, rb = max(0, r0 - overlap_px), min(height, r0 + tile_px + overlap_px)
            ca, cb = max(0, c0 - overlap_px), min(width, c0 + tile_px + overlap_px)
            tiles.append(
                TileSpec(
                    row_off=ra,
                    col_off=ca,
                    height=rb - ra,
                    width=cb - ca,
                    core_rows=(r0 - ra, min(height, r0 + tile_px) - ra),
                    core_cols=(c0 - ca, min(width, c0 + tile_px) - ca),
                )
            )
    return tiles


@dataclass
class CoarseArea:
    """Drainage area (m²) from a decimated read of a whole mosaic, so tiles see the
    true upstream catchment instead of one truncated at the tile edge."""

    area_m2: np.ndarray
    transform: tuple[float, ...]

    def sample(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        inv = ~Affine(*self.transform[:6])
        cols, rows = inv * (np.asarray(xs), np.asarray(ys))
        r = np.clip(np.floor(rows).astype(np.int64), 0, self.area_m2.shape[0] - 1)
        c = np.clip(np.floor(cols).astype(np.int64), 0, self.area_m2.shape[1] - 1)
        return self.area_m2[r, c]


def compute_coarse_area(dem_path: str | Path, cfg: ReachConfig, decimation: int = 4) -> CoarseArea:
    with rasterio.open(dem_path) as src:
        shape = (max(1, src.height // decimation), max(1, src.width // decimation))
        elev = src.read(1, out_shape=shape, masked=True).filled(np.nan).astype(np.float32)
        transform = src.transform * Affine.scale(src.width / shape[1], src.height / shape[0])
    px = abs(transform.a)
    _, acc = compute_flow_dir_accum(elev, transform, fill_depressions=cfg.fill_depressions)
    area = acc * np.float32(px * abs(transform.e))
    # Coarse thalwegs sit a few meters off the 1 m ones: take a 3x3 local max.
    area = ndi.maximum_filter(area, size=3)
    return CoarseArea(area_m2=area.astype(np.float32), transform=tuple(transform)[:6])


@dataclass
class MosaicReaches:
    """Reach network + features + labels for one DEM. Saved/loaded as one ``.npz``."""

    stem: str
    dem_path: str
    crs_wkt: str
    transform: tuple[float, ...]
    shape: tuple[int, int]
    pixel_size: float
    pix_r: np.ndarray  # int32, concatenated reach pixels (upstream → downstream)
    pix_c: np.ndarray
    reach_ptr: np.ndarray  # int64 (n + 1) offsets into pix_*
    down: np.ndarray  # int32 downstream reach id, -1 at outlets
    main_up: np.ndarray  # int32 largest-area upstream reach id, -1 at heads
    length_m: np.ndarray
    features: dict[str, np.ndarray]
    label: np.ndarray  # int8: 1 positive, 0 negative, -1 ignore
    route_frac: np.ndarray  # fraction of reach pixels within label_tol_m of a track
    drop_r: np.ndarray  # profile-step candidates (pixel row/col, size, reach id)
    drop_c: np.ndarray
    drop_m: np.ndarray
    drop_excess_m: np.ndarray
    drop_reach: np.ndarray
    core: np.ndarray  # bool: reach midpoint inside the tile core (always True untiled)
    group: str = ""  # CV group (mosaics whose extents overlap share one)
    meta: dict = field(default_factory=dict)
    # Set by label_reaches (not cached): index into canyon_keys of each positive's track.
    canyon: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    canyon_keys: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.length_m)

    @property
    def affine(self) -> Affine:
        return Affine(*self.transform[:6])

    def reach_pixels(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        a, b = self.reach_ptr[i], self.reach_ptr[i + 1]
        return self.pix_r[a:b], self.pix_c[a:b]

    def per_pixel_reach(self) -> np.ndarray:
        """Reach id for every entry of ``pix_r``/``pix_c``."""
        return np.repeat(np.arange(self.n, dtype=np.int32), np.diff(self.reach_ptr))

    def save(self, path: Path) -> None:
        arrays = {
            k: getattr(self, k)
            for k in (
                "pix_r",
                "pix_c",
                "reach_ptr",
                "down",
                "main_up",
                "length_m",
                "label",
                "route_frac",
                "drop_r",
                "drop_c",
                "drop_m",
                "drop_excess_m",
                "drop_reach",
                "core",
            )
        }
        arrays.update({f"feat__{k}": v for k, v in self.features.items()})
        header = {
            "stem": self.stem,
            "dem_path": self.dem_path,
            "crs_wkt": self.crs_wkt,
            "transform": list(self.transform),
            "shape": list(self.shape),
            "pixel_size": self.pixel_size,
            "group": self.group,
            "meta": self.meta,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part.npz")
        np.savez_compressed(tmp, header=np.array(json.dumps(header)), **arrays)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> MosaicReaches:
        with np.load(path, allow_pickle=False) as z:
            header = json.loads(str(z["header"]))
            arrays = {k: z[k] for k in z.files if k != "header"}
        features = {
            k.removeprefix("feat__"): arrays.pop(k) for k in list(arrays) if k.startswith("feat__")
        }
        return cls(
            stem=header["stem"],
            dem_path=header["dem_path"],
            crs_wkt=header["crs_wkt"],
            transform=tuple(header["transform"]),
            shape=tuple(header["shape"]),
            pixel_size=float(header["pixel_size"]),
            group=header.get("group", ""),
            features=features,
            meta=header.get("meta", {}),
            **arrays,
        )

    def to_gdf(self, columns: dict[str, np.ndarray] | None = None) -> gpd.GeoDataFrame:
        """Reach LineStrings (joined to the downstream reach) with features/labels."""
        geoms = []
        for i in range(self.n):
            r, c = self.reach_pixels(i)
            j = int(self.down[i])
            if j >= 0:
                jr, jc = self.reach_pixels(j)
                r, c = np.append(r, jr[0]), np.append(c, jc[0])
            xs, ys = rio_xy(self.affine, r, c)
            pts = list(zip(np.atleast_1d(xs), np.atleast_1d(ys), strict=True))
            geoms.append(LineString(pts) if len(pts) >= 2 else Point(pts[0]))
        data = {
            "reach": np.arange(self.n),
            "down": self.down,
            "label": self.label,
            "core": self.core,
            "route_frac": self.route_frac,
            "length_m": self.length_m,
            **self.features,
            **(columns or {}),
        }
        return gpd.GeoDataFrame(data, geometry=geoms, crs=CRS.from_wkt(self.crs_wkt))

    def drops_gdf(self) -> gpd.GeoDataFrame:
        xs, ys = rio_xy(self.affine, self.drop_r, self.drop_c)
        geoms = [Point(x, y) for x, y in zip(np.atleast_1d(xs), np.atleast_1d(ys), strict=True)]
        return gpd.GeoDataFrame(
            {
                "reach": self.drop_reach,
                "drop_m": self.drop_m,
                "excess_m": self.drop_excess_m,
                "label": self.label[self.drop_reach],
            },
            geometry=geoms,
            crs=CRS.from_wkt(self.crs_wkt),
        )


# ---------------------------------------------------------------------------
# Network tracing
# ---------------------------------------------------------------------------


def _downstream_index(fdir: np.ndarray, stream: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Flat indices of stream pixels and, per pixel, the position of its D8 receiver
    in that array (-1 when it leaves the network or the raster)."""
    h, w = stream.shape
    flat = np.flatnonzero(stream)
    r, c = np.divmod(flat, w)
    code = fdir.ravel()[flat]
    nr = np.full_like(r, -1)
    nc = np.full_like(c, -1)
    for code_val, (dy, dx) in D8_OFFSETS.items():
        m = code == code_val
        nr[m] = r[m] + dy
        nc[m] = c[m] + dx
    ok = (nr >= 0) & (nr < h) & (nc >= 0) & (nc < w)
    tgt = np.where(ok, nr * w + nc, 0)
    ok &= stream.ravel()[tgt]
    down = np.full(len(flat), -1, dtype=np.int64)
    down[ok] = np.searchsorted(flat, tgt[ok])
    return flat, down


def _trace_links(down: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    """Split the stream forest into links: each starts at a head (no donors) or a
    junction (≥ 2 donors) and runs downstream until the next junction or outlet.

    Returns (links as arrays of stream-pixel positions, receiving link id or -1).
    """
    n = len(down)
    donors = np.bincount(down[down >= 0], minlength=n)
    starts = np.flatnonzero(donors != 1)
    link_of_start = np.full(n, -1, dtype=np.int64)
    link_of_start[starts] = np.arange(len(starts))
    links: list[np.ndarray] = []
    end_pixel = np.full(len(starts), -1, dtype=np.int64)
    down_l = down.tolist()
    donors_l = donors.tolist()
    for k, s in enumerate(starts.tolist()):
        path = [s]
        j = down_l[s]
        while j >= 0 and donors_l[j] == 1 and len(path) < n:
            path.append(j)
            j = down_l[j]
        links.append(np.asarray(path, dtype=np.int64))
        end_pixel[k] = j
    link_down = np.where(end_pixel >= 0, link_of_start[np.maximum(end_pixel, 0)], -1)
    return links, link_down


def _step_lengths(r: np.ndarray, c: np.ndarray, pixel_size: float) -> np.ndarray:
    """Per-pixel step length to the next pixel (last pixel gets one pixel)."""
    d = np.hypot(np.diff(r), np.diff(c)) * pixel_size
    return np.append(d, pixel_size)


# ---------------------------------------------------------------------------
# Profile steps
# ---------------------------------------------------------------------------


def _forward_index(dist: np.ndarray, window_m: float) -> np.ndarray:
    """For each i, the first index j with dist[j] - dist[i] >= window (clamped to end)."""
    return np.minimum(np.searchsorted(dist, dist + window_m, side="left"), len(dist) - 1)


def _profile_steps(
    z: np.ndarray, dist: np.ndarray, n_own: int, cfg: ReachConfig
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Drop over ``drop_window_m`` and its excess over the local grade, plus NMS
    step candidates among the first ``n_own`` profile indices."""
    j = _forward_index(dist, cfg.drop_window_m)
    run = np.maximum(dist[j] - dist, 1e-6)
    drop = z - z[j]
    jg = _forward_index(dist, cfg.grade_window_m / 2)
    ig = np.maximum(np.searchsorted(dist, dist - cfg.grade_window_m / 2, side="left"), 0).astype(
        np.int64
    )
    grade = (z[ig] - z[jg]) / np.maximum(dist[jg] - dist[ig], 1e-6)
    excess = drop - np.clip(grade, 0.0, None) * run
    cand = np.flatnonzero(drop[:n_own] >= cfg.min_drop_m)
    kept: list[int] = []
    for i in cand[np.argsort(-drop[cand], kind="stable")].tolist():
        if all(abs(dist[i] - dist[k]) >= 2 * cfg.drop_window_m for k in kept):
            kept.append(i)
    return drop[:n_own], excess[:n_own], sorted(kept)


# ---------------------------------------------------------------------------
# Cross sections
# ---------------------------------------------------------------------------


def _cross_sections(
    filled: np.ndarray,
    valid: np.ndarray,
    r0: np.ndarray,
    c0: np.ndarray,
    ur: np.ndarray,
    uc: np.ndarray,
    pixel_size: float,
    cfg: ReachConfig,
) -> dict[str, np.ndarray]:
    """Wall heights at fixed offsets and valley widths at fixed rises, per sample.

    ``(ur, uc)`` is the unit perpendicular in (row, col). Each side is a ray of
    1-pixel steps out to ``xs_max_m``; rise is the running max of elevation above the
    thalweg. Wall = min over both sides (a canyon needs two walls); width = sum of
    the per-side distances to the first point that rises ``h`` meters.
    """
    h, w = filled.shape
    n_steps = max(1, round(cfg.xs_max_m / pixel_size))
    t = np.arange(1, n_steps + 1, dtype=np.float32)
    z0 = filled[r0, c0]
    out: dict[str, list[np.ndarray]] = {
        **{f"wall{d:g}": [] for d in cfg.xs_wall_m},
        **{f"width{hh:g}": [] for hh in cfg.xs_width_rise_m},
    }
    chunk = 20_000
    for a in range(0, len(r0), chunk):
        sl = slice(a, a + chunk)
        rises = []
        for side in (1.0, -1.0):
            rr = np.rint(r0[sl, None] + side * t[None, :] * ur[sl, None]).astype(np.int64)
            cc = np.rint(c0[sl, None] + side * t[None, :] * uc[sl, None]).astype(np.int64)
            inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
            rr, cc = np.clip(rr, 0, h - 1), np.clip(cc, 0, w - 1)
            z = np.where(inb & valid[rr, cc], filled[rr, cc], np.nan)
            rise = np.fmax.accumulate(z - z0[sl, None], axis=1)
            rises.append(rise)
        for d in cfg.xs_wall_m:
            k = min(n_steps, max(1, round(d / pixel_size))) - 1
            out[f"wall{d:g}"].append(np.fmin(rises[0][:, k], rises[1][:, k]))
        for hh in cfg.xs_width_rise_m:
            widths = []
            for rise in rises:
                hit = rise >= hh
                first = np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, n_steps)
                widths.append(first.astype(np.float32) * pixel_size)
            out[f"width{hh:g}"].append(widths[0] + widths[1])
    return {k: np.concatenate(v) if v else np.zeros(0, np.float32) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


# A label track: a KML path (its descent lines are used) or prepared WGS84 lines,
# e.g. only the technical section of a descent. Lines may set ``attrs["key"]``.
Track = Path | gpd.GeoDataFrame


def track_lines(track: Track) -> gpd.GeoDataFrame:
    return track if isinstance(track, gpd.GeoDataFrame) else parse_kml(track).descent_lines


def track_key(track: Track) -> str:
    return track.attrs.get("key", "") if isinstance(track, gpd.GeoDataFrame) else str(track)


def _track_distance_m(
    tracks: list[Track], transform, crs, shape: tuple[int, int], pixel_size: float
) -> tuple[np.ndarray, np.ndarray] | None:
    """(distance in m to the nearest track line, index into ``tracks`` of that line)
    for every pixel, or None if there are no lines."""
    owner = np.full(shape, -1, dtype=np.int32)
    for i, t in enumerate(tracks):
        owner[_rasterize_lines(track_lines(t), transform, crs, shape) > 0] = i
    if not (owner >= 0).any():
        return None
    dist, (iy, ix) = ndi.distance_transform_edt(owner < 0, return_indices=True)
    return (dist * pixel_size).astype(np.float32), owner[iy, ix]


def _reach_route_frac(dist_m: np.ndarray | None, mr: MosaicReaches, tol_m: float) -> np.ndarray:
    if dist_m is None:
        return np.zeros(mr.n, dtype=np.float32)
    dist_m = dist_m[0]
    near = (dist_m[mr.pix_r, mr.pix_c] <= tol_m).astype(np.float64)
    sums = np.add.reduceat(near, mr.reach_ptr[:-1]) if len(near) else np.zeros(mr.n)
    return (sums / np.maximum(np.diff(mr.reach_ptr), 1)).astype(np.float32)


def label_reaches(
    mr: MosaicReaches,
    positive_kmls: list[Track],
    cfg: ReachConfig,
    *,
    ignore_kmls: list[Track] | None = None,
    ignore_points_xy: list[tuple[float, float]] | None = None,
) -> None:
    """Assign ``label``/``route_frac`` in place.

    - positive: ≥ ``label_min_frac`` of the reach within ``label_tol_m`` of a track
    - ignore: partial overlap; main-stem neighbors of a positive run (route ends are
      fuzzy and canyons often continue past the mapped descent); reaches overlapping
      ``ignore_kmls`` (e.g. non-technical routes); reaches within
      ``ignore_radius_m`` of ``ignore_points_xy`` (known canyons with no track)
    - negative: everything else
    """
    tr = mr.affine
    crs = CRS.from_wkt(mr.crs_wkt)
    dist = _track_distance_m(positive_kmls, tr, crs, mr.shape, mr.pixel_size)
    frac = _reach_route_frac(dist, mr, cfg.label_tol_m)
    # Which track each reach is nearest to (at its midpoint), for per-canyon weighting.
    mid = mr.reach_ptr[:-1] + np.diff(mr.reach_ptr) // 2
    canyon = dist[1][mr.pix_r[mid], mr.pix_c[mid]] if dist is not None else np.full(mr.n, -1)
    label = np.zeros(mr.n, dtype=np.int8)
    label[frac > 0] = -1
    pos = frac >= cfg.label_min_frac
    label[pos] = 1

    for nb in (mr.down, mr.main_up):
        idx = nb[pos]
        idx = idx[idx >= 0]
        label[idx[label[idx] == 0]] = -1

    if ignore_kmls:
        d_ign = _track_distance_m(ignore_kmls, tr, crs, mr.shape, mr.pixel_size)
        f_ign = _reach_route_frac(d_ign, mr, cfg.label_tol_m)
        label[(f_ign > 0) & (label == 0)] = -1

    if ignore_points_xy:
        xs, ys = rio_xy(tr, mr.pix_r, mr.pix_c)
        xs, ys = np.asarray(xs), np.asarray(ys)
        near = np.zeros(len(xs), dtype=bool)
        for px, py in ignore_points_xy:
            near |= np.hypot(xs - px, ys - py) <= cfg.ignore_radius_m
        hit = np.add.reduceat(near.astype(np.int64), mr.reach_ptr[:-1]) > 0
        label[hit & (label == 0)] = -1

    label[~mr.core] = -1
    mr.label = label
    mr.route_frac = frac
    mr.canyon = np.where(label == 1, canyon, -1).astype(np.int32)
    mr.canyon_keys = [track_key(k) for k in positive_kmls]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _ctx_mean(values: np.ndarray, weight: np.ndarray, down: np.ndarray, up: np.ndarray, hops: int):
    """Length-weighted mean over the reach plus ``hops`` main-stem reaches each way."""
    acc = values * weight
    wsum = weight.copy()
    for nb in (down, up):
        idx = np.arange(len(values))
        for _ in range(hops):
            idx = np.where(idx >= 0, nb[np.maximum(idx, 0)], -1)
            ok = idx >= 0
            acc[ok] += values[idx[ok]] * weight[idx[ok]]
            wsum[ok] += weight[idx[ok]]
    return acc / np.maximum(wsum, 1e-6)


def _read_dem_window(path: Path, window: Window):
    with rasterio.open(path) as src:
        elev = src.read(1, window=window, masked=True).filled(np.nan).astype(np.float32)
        transform = src.window_transform(window)
        crs = src.crs
    log(f"DEM window loaded: {path.name} {window}")
    return elev, transform, crs, abs(transform.a)


def build_reaches(
    dem_path: str | Path,
    cfg: ReachConfig,
    *,
    tile: TileSpec | None = None,
    coarse_area: CoarseArea | None = None,
) -> MosaicReaches:
    """Trace the network and compute features (labels left as all-negative)."""
    dem_path = Path(dem_path)
    if tile is None:
        elev, transform, crs, px = read_dem(str(dem_path))
    else:
        elev, transform, crs, px = _read_dem_window(dem_path, tile.window)
    valid = np.isfinite(elev)
    filled = fill_nans(elev)

    fdir, acc = compute_flow_dir_accum(elev, transform, fill_depressions=cfg.fill_depressions)
    area = acc * np.float32(px * px)
    del acc
    stream = (area >= cfg.min_area_m2) & valid
    if not stream.any():
        raise ValueError(f"{dem_path.name}: no stream pixels at min_area_m2={cfg.min_area_m2}")
    flat, pdown = _downstream_index(fdir, stream)
    del fdir
    h, w = elev.shape
    pr, pc = np.divmod(flat, w)
    links, link_down = _trace_links(pdown)
    log(f"reaches: {len(flat)} stream px, {len(links)} links")

    # Split links into reaches; reach ids are assigned link by link, upstream → down.
    reach_pixels: list[np.ndarray] = []
    reach_link: list[int] = []
    link_first_reach = np.zeros(len(links), dtype=np.int64)
    for k, lk in enumerate(links):
        step = _step_lengths(pr[lk], pc[lk], px)
        length = float(step.sum())
        n_chunks = max(1, round(length / cfg.reach_length_m))
        dist = np.concatenate([[0.0], np.cumsum(step)[:-1]])
        chunk_of = np.minimum((dist / (length / n_chunks)).astype(np.int64), n_chunks - 1)
        bounds = np.flatnonzero(np.diff(chunk_of)) + 1
        link_first_reach[k] = len(reach_pixels)
        for part in np.split(lk, bounds):
            reach_pixels.append(part)
            reach_link.append(k)
    n = len(reach_pixels)
    reach_link_arr = np.asarray(reach_link, dtype=np.int64)
    down = np.arange(1, n + 1, dtype=np.int64)
    last_of_link = np.append(link_first_reach[1:], n) - 1
    ld = link_down[reach_link_arr[last_of_link]]
    down[last_of_link] = np.where(ld >= 0, link_first_reach[np.maximum(ld, 0)], -1)

    sizes = np.array([len(p) for p in reach_pixels], dtype=np.int64)
    reach_ptr = np.concatenate([[0], np.cumsum(sizes)])
    allpix = np.concatenate(reach_pixels) if n else np.zeros(0, np.int64)
    pix_r = pr[allpix].astype(np.int32)
    pix_c = pc[allpix].astype(np.int32)
    pix_reach = np.repeat(np.arange(n), sizes)

    def reach_mean(v: np.ndarray) -> np.ndarray:
        return (np.add.reduceat(v.astype(np.float64), reach_ptr[:-1]) / sizes).astype(np.float32)

    def reach_max(v: np.ndarray) -> np.ndarray:
        return np.maximum.reduceat(v, reach_ptr[:-1]).astype(np.float32)

    reach_area = reach_max(area[pix_r, pix_c])
    del area
    if coarse_area is not None:
        xs, ys = rio_xy(transform, pix_r, pix_c)
        reach_area = np.maximum(reach_area, reach_max(coarse_area.sample(xs, ys)))
    main_up = np.full(n, -1, dtype=np.int64)
    for i in np.argsort(reach_area, kind="stable").tolist():
        if down[i] >= 0:
            main_up[down[i]] = i  # ascending area → largest donor wins

    feats: dict[str, np.ndarray] = {"log_area": np.log10(np.maximum(reach_area, 1.0))}
    for rad in cfg.depth_radii_m:
        neg_tpi = -compute_tpi(elev, px, rad, filled=filled)
        feats[f"depth{rad:g}"] = reach_mean(np.nan_to_num(neg_tpi[pix_r, pix_c]))
        del neg_tpi

    # Per-link profiles (extended downstream so windows don't truncate at junctions).
    z_pix = filled[pix_r, pix_c].astype(np.float64)
    step_pix = np.zeros(len(allpix), dtype=np.float64)
    drop_pix = np.zeros(len(allpix), dtype=np.float32)
    excess_pix = np.zeros(len(allpix), dtype=np.float32)
    dir_r = np.zeros(len(allpix), dtype=np.float32)
    dir_c = np.zeros(len(allpix), dtype=np.float32)
    ext_px = max(cfg.drop_window_m, cfg.grade_window_m / 2) / px + 2
    drop_rows: list[tuple[int, float, float]] = []
    half = max(1, round(cfg.xs_dir_half_window_m / px))
    offset = 0
    for lk in links:
        ext = list(lk)
        j = int(pdown[lk[-1]])
        while j >= 0 and len(ext) - len(lk) < ext_px:
            ext.append(j)
            j = int(pdown[j])
        ext_arr = np.asarray(ext)
        er, ec = pr[ext_arr], pc[ext_arr]
        step = _step_lengths(er, ec, px)
        dist = np.concatenate([[0.0], np.cumsum(step)[:-1]])
        z = filled[er, ec].astype(np.float64)
        m = len(lk)
        drop, excess, kept = _profile_steps(z, dist, m, cfg)
        sl = slice(offset, offset + m)
        step_pix[sl] = step[:m]
        drop_pix[sl] = drop
        excess_pix[sl] = excess
        ia = np.clip(np.arange(m) - half, 0, len(ext_arr) - 1)
        ib = np.clip(np.arange(m) + half, 0, len(ext_arr) - 1)
        dr = (er[ib] - er[ia]).astype(np.float32)
        dc = (ec[ib] - ec[ia]).astype(np.float32)
        norm = np.maximum(np.hypot(dr, dc), 1e-6)
        dir_r[sl], dir_c[sl] = dr / norm, dc / norm
        drop_rows.extend((offset + i, float(drop[i]), float(excess[i])) for i in kept)
        offset += m

    length_m = np.add.reduceat(step_pix, reach_ptr[:-1]).astype(np.float32)
    z_first = z_pix[reach_ptr[:-1]]
    z_last = np.where(down >= 0, z_pix[reach_ptr[np.maximum(down, 0)]], z_pix[reach_ptr[1:] - 1])
    feats["gradient"] = ((z_first - z_last) / np.maximum(length_m, 1e-6)).astype(np.float32)
    feats["max_drop"] = reach_max(drop_pix)
    feats["max_step_excess"] = reach_max(excess_pix)
    drop_idx = np.asarray([d[0] for d in drop_rows], dtype=np.int64)
    drop_reach = pix_reach[drop_idx].astype(np.int32) if len(drop_idx) else np.zeros(0, np.int32)
    n_drops = np.bincount(drop_reach, minlength=n).astype(np.float32)
    feats["drops_per_km"] = n_drops / np.maximum(length_m, 1.0) * 1000.0

    # Cross sections every xs_spacing_m along each reach (always ≥ 1 per reach).
    spacing = max(1, round(cfg.xs_spacing_m / px))
    within = np.arange(len(allpix)) - reach_ptr[pix_reach]
    mid = (sizes // 2)[pix_reach]
    sample = within % spacing == mid % spacing  # always includes each reach's midpoint
    s_idx = np.flatnonzero(sample)
    xs = _cross_sections(
        filled,
        valid,
        pix_r[s_idx],
        pix_c[s_idx],
        -dir_c[s_idx],  # perpendicular of (dr, dc) is (-dc, dr)
        dir_r[s_idx],
        px,
        cfg,
    )
    s_reach = pix_reach[s_idx]
    order = np.argsort(s_reach, kind="stable")
    s_ptr = np.searchsorted(s_reach[order], np.arange(n + 1))
    for name, vals in xs.items():
        vals = vals[order]
        med = np.full(n, np.nan, dtype=np.float32)
        for i in range(n):
            seg = vals[s_ptr[i] : s_ptr[i + 1]]
            seg = seg[np.isfinite(seg)]
            if len(seg):
                med[i] = np.median(seg)
        feats[name] = med
    for hh in cfg.xs_width_rise_m:
        feats[f"log_width{hh:g}"] = np.log1p(feats[f"width{hh:g}"])
    if "wall50" in feats and "width20" in feats:
        feats["wall50_per_width20"] = feats["wall50"] / np.maximum(feats["width20"], 1.0)

    weight = length_m.astype(np.float64)
    for name in CONTEXT_FEATURES:
        if name in feats:
            v = np.nan_to_num(feats[name].astype(np.float64))
            feats[f"ctx_{name}"] = _ctx_mean(v, weight, down, main_up, cfg.context_hops).astype(
                np.float32
            )

    drop_idx_arr = drop_idx if len(drop_idx) else np.zeros(0, np.int64)
    core = np.ones(n, dtype=bool)
    if tile is not None:
        mid = reach_ptr[:-1] + sizes // 2
        mr_, mc_ = pix_r[mid], pix_c[mid]
        core = (
            (mr_ >= tile.core_rows[0])
            & (mr_ < tile.core_rows[1])
            & (mc_ >= tile.core_cols[0])
            & (mc_ < tile.core_cols[1])
        )
    return MosaicReaches(
        stem=dem_path.stem + (tile.suffix if tile else ""),
        dem_path=str(dem_path),
        crs_wkt=crs.to_wkt(),
        transform=tuple(transform)[:6],
        shape=(h, w),
        pixel_size=float(px),
        pix_r=pix_r,
        pix_c=pix_c,
        reach_ptr=reach_ptr,
        down=down.astype(np.int32),
        main_up=main_up.astype(np.int32),
        length_m=length_m,
        features=feats,
        label=np.zeros(n, dtype=np.int8),
        route_frac=np.zeros(n, dtype=np.float32),
        drop_r=pix_r[drop_idx_arr],
        drop_c=pix_c[drop_idx_arr],
        drop_m=np.asarray([d[1] for d in drop_rows], dtype=np.float32),
        drop_excess_m=np.asarray([d[2] for d in drop_rows], dtype=np.float32),
        drop_reach=drop_reach,
        core=core,
        meta={"config": asdict(cfg), "signature": cfg.signature()},
    )


def load_or_build_reaches(
    dem_path: str | Path,
    cfg: ReachConfig,
    cache_dir: str | Path,
    *,
    tile: TileSpec | None = None,
    coarse_area: CoarseArea | None = None,
) -> MosaicReaches:
    """Cached :func:`build_reaches` keyed by DEM stem (+ tile) + config signature."""
    dem_path = Path(dem_path)
    stem = dem_path.stem + (tile.suffix if tile else "")
    path = Path(cache_dir) / f"{stem}.{cfg.signature()}.npz"
    if path.exists():
        return MosaicReaches.load(path)
    mr = build_reaches(dem_path, cfg, tile=tile, coarse_area=coarse_area)
    mr.save(path)
    return mr

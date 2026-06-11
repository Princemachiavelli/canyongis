"""GeoNet-style analytical canyon baseline — no training required.

Pipeline (after Passalacqua et al., "GeoNet: automatic extraction of channel
heads and networks from high-resolution topography"):

1. **Perona-Malik** nonlinear diffusion smooths the DEM while preserving the
   sharp hillslope-to-valley transitions that box/Gaussian filters blur away.
2. **Laplacian curvature** on the filtered DEM; pixels above a quantile
   threshold are candidate channelized terrain (concave-up).
3. **D8 flow directions/accumulation** (pysheds) define the drainage network.
   A channel head is the first pixel along the flow path from each network
   source where the curvature criterion is met.
4. Paths are traced downstream from each head — longest first; tributaries
   stop where they join an already-traced path.
5. Each centerline is scored for *canyon* character: confinement depth
   (negative TPI), grade, and sudden-drop (rappel candidate) detection along
   the raw elevation profile.

Outputs mirror canyongis-ml-predict (centerlines + drop points) so the two
can be compared directly. See ML_FEATURE_REVIEW.md §4 for motivation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path

import geopandas as gpd
import numpy as np
from rasterio.transform import xy as rio_xy
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point

from .hydrology import D8_OFFSETS, compute_flow_dir_accum
from .io import log, read_dem, write_raster, write_vector
from .terrain import compute_tpi, fill_nans


@dataclass(frozen=True)
class BaselineConfig:
    # Perona-Malik smoothing
    pm_iters: int = 50
    pm_dt: float = 0.1
    pm_kappa: float = 0.0  # edge-stop threshold (m/px); 0 = auto from gradient quantile
    pm_kappa_quantile: float = 0.9

    # Channelization criterion
    curv_quantile: float = 0.9
    min_area_m2: float = 50_000.0
    fill_depressions: bool = True

    # Canyon scoring
    depth_radius_m: float = 150.0  # negative-TPI radius used as confinement depth
    confine_depth_m: float = 10.0  # depth above which a pixel counts as "confined"
    min_length_m: float = 100.0

    # Drop (rappel candidate) detection along the profile
    drop_window_m: float = 10.0
    min_drop_m: float = 3.0

    # Safety cap on total traced pixels across all heads
    max_steps: int = 5_000_000


@dataclass(frozen=True)
class BaselineResult:
    out_dir: str
    n_heads: int
    n_centerlines: int
    n_drops: int
    centerlines_path: str
    drops_path: str
    centerlines_kml_path: str
    drops_kml_path: str
    summary_path: str


def perona_malik(
    elev: np.ndarray,
    *,
    iters: int = 50,
    dt: float = 0.1,
    kappa: float | None = None,
    kappa_quantile: float = 0.9,
) -> np.ndarray:
    """Perona-Malik (1990) anisotropic diffusion with the 1/(1+(d/k)^2) stopper.

    Differences are in elevation units per pixel step, so `kappa` is too.
    When `kappa` is None it is estimated as the `kappa_quantile` quantile of
    the gradient magnitude (GeoNet's heuristic): edges steeper than ~that
    quantile are preserved, gentler texture diffuses away.
    """
    z = np.asarray(elev, dtype=np.float32).copy()
    if kappa is None:
        gy, gx = np.gradient(z)
        mag = np.hypot(gy, gx)
        kappa = float(np.quantile(mag[np.isfinite(mag)], kappa_quantile))
    kappa = max(float(kappa), 1e-6)

    dn = np.zeros_like(z)
    ds = np.zeros_like(z)
    de = np.zeros_like(z)
    dw = np.zeros_like(z)
    for _ in range(max(0, iters)):
        dn[1:, :] = z[:-1, :] - z[1:, :]
        ds[:-1, :] = z[1:, :] - z[:-1, :]
        de[:, :-1] = z[:, 1:] - z[:, :-1]
        dw[:, 1:] = z[:, :-1] - z[:, 1:]
        flux = sum(d / (1.0 + (d / kappa) ** 2) for d in (dn, ds, de, dw))
        z += np.float32(dt) * flux
    return z


def laplacian_curvature(smoothed: np.ndarray, pixel_size: float) -> np.ndarray:
    """∇²z in 1/m; positive in concave-up (channelized) terrain."""
    return (ndi.laplace(smoothed) / np.float32(pixel_size) ** 2).astype(np.float32)


def _find_heads(fdir: np.ndarray, stream: np.ndarray) -> list[tuple[int, int]]:
    """Stream pixels with no upstream stream donor — sources of the network."""
    h, w = stream.shape
    donors = np.zeros(stream.shape, dtype=np.int16)
    for code, (dy, dx) in D8_OFFSETS.items():
        ys, xs = np.where(stream & (fdir == code))
        py, px = ys + dy, xs + dx
        ok = (py >= 0) & (py < h) & (px >= 0) & (px < w)
        np.add.at(donors, (py[ok], px[ok]), 1)
    ys, xs = np.where(stream & (donors == 0))
    return list(zip(ys.tolist(), xs.tolist(), strict=True))


def _walk_downstream(
    fdir: np.ndarray, stream: np.ndarray, r: int, c: int, max_steps: int
) -> list[tuple[int, int]]:
    """Follow D8 directions from (r, c) while on the stream network."""
    h, w = stream.shape
    path = [(r, c)]
    while len(path) < max_steps:
        off = D8_OFFSETS.get(int(fdir[r, c]))
        if off is None:
            break
        nr, nc = r + off[0], c + off[1]
        if not (0 <= nr < h and 0 <= nc < w) or not stream[nr, nc]:
            break
        path.append((nr, nc))
        r, c = nr, nc
    return path


def _trace_network(
    fdir: np.ndarray,
    stream: np.ndarray,
    curv_mask: np.ndarray,
    heads: list[tuple[int, int]],
    cfg: BaselineConfig,
) -> list[list[tuple[int, int]]]:
    """Trace flow paths from each head; start each at its channel head.

    The channel head is the first pixel along the path satisfying the
    curvature criterion (GeoNet's definition). Paths are claimed longest
    first so trunks stay whole and tributaries end at their junction pixel.
    """
    all_paths: list[list[tuple[int, int]]] = []
    total = 0
    for r, c in heads:
        path = _walk_downstream(fdir, stream, r, c, cfg.max_steps - total)
        total += len(path)
        all_paths.append(path)
        if total >= cfg.max_steps:
            log(f"baseline: hit max_steps={cfg.max_steps}; some heads were not traced")
            break

    all_paths.sort(key=len, reverse=True)
    visited = np.zeros(stream.shape, dtype=bool)
    kept: list[list[tuple[int, int]]] = []
    for path in all_paths:
        start = next((i for i, (r, c) in enumerate(path) if curv_mask[r, c]), None)
        if start is None:
            continue
        sub: list[tuple[int, int]] = []
        for r, c in path[start:]:
            sub.append((r, c))
            if visited[r, c]:
                break  # junction with an already-claimed path
        if len(sub) < 2:
            continue
        for r, c in sub:
            visited[r, c] = True
        kept.append(sub)
    return kept


def _detect_drops(
    profile: np.ndarray, dists: np.ndarray, cfg: BaselineConfig, pixel_size: float
) -> list[tuple[int, float, float]]:
    """Sudden elevation drops along a profile → [(index, drop_m, grade)].

    Drop is measured over a forward window of `drop_window_m`; candidates are
    kept by descending drop with `2 * drop_window_m` non-max suppression.
    """
    n = len(profile)
    if n < 2:
        return []
    w = max(1, round(cfg.drop_window_m / max(pixel_size, 1e-6)))
    ahead = np.minimum(np.arange(n) + w, n - 1)
    drop = profile - profile[ahead]
    run = np.maximum(dists[ahead] - dists, 1e-6)
    candidates = np.where(drop >= cfg.min_drop_m)[0]
    kept: list[int] = []
    for i in sorted(candidates.tolist(), key=lambda k: -drop[k]):
        if all(abs(dists[i] - dists[k]) >= 2 * cfg.drop_window_m for k in kept):
            kept.append(i)
    return [(int(i), float(drop[i]), float(drop[i] / run[i])) for i in sorted(kept)]


def _path_distances(path: list[tuple[int, int]], pixel_size: float) -> np.ndarray:
    """Cumulative along-path distance in meters (diagonal steps count √2)."""
    steps = [0.0]
    for (r0, c0), (r1, c1) in pairwise(path):
        steps.append(float(np.hypot(r1 - r0, c1 - c0)) * pixel_size)
    return np.cumsum(np.asarray(steps, dtype=np.float64))


def run_baseline(
    dem_path: str | Path, out_dir: str | Path, cfg: BaselineConfig | None = None
) -> BaselineResult:
    cfg = cfg or BaselineConfig()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    elev, transform, crs, pixel_size = read_dem(str(dem_path))
    valid = np.isfinite(elev)
    filled = fill_nans(elev)

    log(f"baseline: Perona-Malik smoothing ({cfg.pm_iters} iters)")
    smoothed = perona_malik(
        filled,
        iters=cfg.pm_iters,
        dt=cfg.pm_dt,
        kappa=cfg.pm_kappa if cfg.pm_kappa > 0 else None,
        kappa_quantile=cfg.pm_kappa_quantile,
    )
    curv = laplacian_curvature(smoothed, pixel_size)
    curv_threshold = float(np.quantile(curv[valid], cfg.curv_quantile))
    curv_mask = (curv >= curv_threshold) & valid

    log("baseline: D8 flow directions / accumulation")
    fdir, acc = compute_flow_dir_accum(elev, transform, fill_depressions=cfg.fill_depressions)
    stream = (acc * pixel_size**2 >= cfg.min_area_m2) & valid

    depth = np.clip(-compute_tpi(elev, pixel_size, cfg.depth_radius_m, filled=filled), 0.0, None)
    depth = np.nan_to_num(depth, nan=0.0)

    heads = _find_heads(fdir, stream)
    log(f"baseline: {int(stream.sum())} stream px, {len(heads)} network sources")
    paths = _trace_network(fdir, stream, curv_mask, heads, cfg)

    entries: list[dict] = []
    for path in paths:
        dists = _path_distances(path, pixel_size)
        length_m = float(dists[-1])
        if length_m < cfg.min_length_m:
            continue
        rows = np.array([p[0] for p in path])
        cols = np.array([p[1] for p in path])
        profile = filled[rows, cols].astype(np.float64)
        path_depth = depth[rows, cols]
        drops = _detect_drops(profile, dists, cfg, pixel_size)
        mean_depth = float(path_depth.mean())
        confined_frac = float((path_depth >= cfg.confine_depth_m).mean())
        entries.append(
            {
                "path": path,
                "dists": dists,
                "drops": drops,
                "length_m": length_m,
                "net_drop_m": float(profile[0] - profile[-1]),
                "grade": float((profile[0] - profile[-1]) / max(length_m, 1e-6)),
                "mean_depth_m": mean_depth,
                "max_depth_m": float(path_depth.max()),
                "confined_frac": confined_frac,
                "n_drops": len(drops),
                "canyon_score": mean_depth * confined_frac,
            }
        )
    entries.sort(key=lambda e: -e["canyon_score"])

    line_rows: list[dict] = []
    drop_rows: list[dict] = []
    for rank, e in enumerate(entries, start=1):
        name = f"C{rank} depth={e['mean_depth_m']:.0f}m score={e['canyon_score']:.1f}"
        coords = [rio_xy(transform, r, c) for r, c in e["path"]]
        line_rows.append(
            {
                "name": name,
                **{
                    k: e[k]
                    for k in (
                        "length_m",
                        "net_drop_m",
                        "grade",
                        "mean_depth_m",
                        "max_depth_m",
                        "confined_frac",
                        "n_drops",
                        "canyon_score",
                    )
                },
                "geometry": LineString(coords),
            }
        )
        for idx, drop_m, grade in e["drops"]:
            r, c = e["path"][idx]
            drop_rows.append(
                {
                    "line": name,
                    "drop_m": drop_m,
                    "grade": grade,
                    "dist_m": float(e["dists"][idx]),
                    "geometry": Point(rio_xy(transform, r, c)),
                }
            )

    def _gdf(rows: list[dict], columns: list[str]) -> gpd.GeoDataFrame:
        if not rows:
            return gpd.GeoDataFrame(columns=[*columns, "geometry"], geometry="geometry", crs=crs)
        return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

    centerlines = _gdf(
        line_rows,
        [
            "name",
            "length_m",
            "net_drop_m",
            "grade",
            "mean_depth_m",
            "max_depth_m",
            "confined_frac",
            "n_drops",
            "canyon_score",
        ],
    )
    drop_points = _gdf(drop_rows, ["line", "drop_m", "grade", "dist_m"])

    write_raster(out_dir / "pm_curvature.tif", curv, transform, crs, dtype="float32")
    write_raster(out_dir / "depth.tif", depth.astype(np.float32), transform, crs, dtype="float32")
    write_raster(
        out_dir / "channel_mask.tif",
        (stream & curv_mask).astype(np.uint8),
        transform,
        crs,
        dtype="uint8",
    )
    write_vector(centerlines, out_dir / "baseline_centerlines")
    write_vector(drop_points, out_dir / "baseline_drops")

    summary = {
        "dem_path": str(dem_path),
        "config": asdict(cfg),
        "curv_threshold": curv_threshold,
        "n_heads": len(heads),
        "n_centerlines": len(line_rows),
        "n_drops": len(drop_rows),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    log(f"baseline: wrote {len(line_rows)} centerlines, {len(drop_rows)} drop points → {out_dir}")

    return BaselineResult(
        out_dir=str(out_dir),
        n_heads=len(heads),
        n_centerlines=len(line_rows),
        n_drops=len(drop_rows),
        centerlines_path=str(out_dir / "baseline_centerlines.gpkg"),
        drops_path=str(out_dir / "baseline_drops.gpkg"),
        centerlines_kml_path=str(out_dir / "baseline_centerlines.kml"),
        drops_kml_path=str(out_dir / "baseline_drops.kml"),
        summary_path=str(summary_path),
    )


def baseline_cli(argv: list[str] | None = None) -> None:
    defaults = BaselineConfig()
    parser = argparse.ArgumentParser(
        prog="canyongis-baseline",
        description=(
            "GeoNet-style analytical canyon detection: Perona-Malik + curvature channel "
            "heads + D8 flow tracing + confinement scoring. No model checkpoint needed."
        ),
    )
    parser.add_argument("dem", type=Path, help="Input projected DEM GeoTIFF.")
    parser.add_argument("outdir", type=Path, help="Output directory.")
    parser.add_argument("--pm-iters", type=int, default=defaults.pm_iters)
    parser.add_argument("--pm-dt", type=float, default=defaults.pm_dt)
    parser.add_argument(
        "--pm-kappa",
        type=float,
        default=defaults.pm_kappa,
        help="Edge-stop threshold in m/px; 0 = auto from gradient quantile.",
    )
    parser.add_argument("--pm-kappa-quantile", type=float, default=defaults.pm_kappa_quantile)
    parser.add_argument(
        "--curv-quantile",
        type=float,
        default=defaults.curv_quantile,
        help="Curvature quantile above which a pixel counts as channelized.",
    )
    parser.add_argument(
        "--min-area-m2",
        type=float,
        default=defaults.min_area_m2,
        help="Minimum drainage area for the stream network (default: 50000 = 5 ha).",
    )
    parser.add_argument("--no-fill-depressions", action="store_true")
    parser.add_argument("--depth-radius-m", type=float, default=defaults.depth_radius_m)
    parser.add_argument("--confine-depth-m", type=float, default=defaults.confine_depth_m)
    parser.add_argument("--min-length-m", type=float, default=defaults.min_length_m)
    parser.add_argument("--drop-window-m", type=float, default=defaults.drop_window_m)
    parser.add_argument("--min-drop-m", type=float, default=defaults.min_drop_m)
    args = parser.parse_args(argv)

    cfg = BaselineConfig(
        pm_iters=args.pm_iters,
        pm_dt=args.pm_dt,
        pm_kappa=args.pm_kappa,
        pm_kappa_quantile=args.pm_kappa_quantile,
        curv_quantile=args.curv_quantile,
        min_area_m2=args.min_area_m2,
        fill_depressions=not args.no_fill_depressions,
        depth_radius_m=args.depth_radius_m,
        confine_depth_m=args.confine_depth_m,
        min_length_m=args.min_length_m,
        drop_window_m=args.drop_window_m,
        min_drop_m=args.min_drop_m,
    )
    run_baseline(args.dem, args.outdir, cfg)


if __name__ == "__main__":
    baseline_cli()

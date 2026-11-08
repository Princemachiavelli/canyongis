"""Diagnostic: how do known descent tracks relate to the D8 drainage network?

For each manifest mosaic (see DETECTION_STRATEGY.md §3):
  - track → stream distance quantiles at several drainage-area thresholds
  - stream px near a route / all stream px (ceiling precision of "every drainage is
    a canyon")
  - per-feature AUC: stream pixels near the route vs. other stream pixels
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage as ndi
from sklearn.metrics import roc_auc_score

from .hydrology import compute_flow_dir_accum
from .io import log, read_dem
from .ml.labels import _rasterize_lines
from .terrain import compute_tpi, fill_nans
from .validate import parse_kml

DEFAULT_AREAS_M2: tuple[float, ...] = (2_000, 10_000, 50_000, 200_000)


def _auc(pos: np.ndarray, neg: np.ndarray) -> float:
    pos, neg = pos[np.isfinite(pos)], neg[np.isfinite(neg)]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    return float(roc_auc_score(y, np.concatenate([pos, neg])))


def diagnose_mosaic(
    dem_path: Path,
    kml_paths: list[Path],
    *,
    areas_m2: tuple[float, ...] = DEFAULT_AREAS_M2,
    feature_area_m2: float = 10_000,
    near_route_m: float = 30.0,
) -> dict[str, float | int | str]:
    elev, transform, crs, px = read_dem(str(dem_path))
    valid = np.isfinite(elev)
    filled = fill_nans(elev)
    _, acc = compute_flow_dir_accum(elev, transform)
    area = acc * px * px
    route = np.zeros(elev.shape, dtype=bool)
    for k in kml_paths:
        route |= _rasterize_lines(parse_kml(k).descent_lines, transform, crs, elev.shape) > 0
    route &= valid
    rec: dict[str, float | int | str] = {"name": dem_path.name, "route_px": int(route.sum())}
    if not route.any():
        return rec

    near_route = ndi.distance_transform_edt(~route) * px <= near_route_m
    for a in areas_m2:
        stream = (area >= a) & valid
        dr = (ndi.distance_transform_edt(~stream) * px)[route]
        rec[f"d_q50@{a:g}"] = float(np.quantile(dr, 0.5))
        rec[f"d_q90@{a:g}"] = float(np.quantile(dr, 0.9))
        rec[f"within15@{a:g}"] = float((dr <= 15).mean())
        rec[f"stream_px@{a:g}"] = int(stream.sum())
        rec[f"prec_all_streams@{a:g}"] = float((stream & near_route).sum() / max(stream.sum(), 1))

    stream = (area >= feature_area_m2) & valid
    pos_m, neg_m = stream & near_route, stream & ~near_route
    slope = np.hypot(*np.gradient(ndi.gaussian_filter(filled, 2))) / px
    feats = {
        "log_area": np.log10(np.maximum(area, 1)),
        "depth50": -compute_tpi(elev, px, 50, filled=filled),
        "depth150": -compute_tpi(elev, px, 150, filled=filled),
        "depth400": -compute_tpi(elev, px, 400, filled=filled),
        "slope": slope,
        "slope30": ndi.uniform_filter(slope, 31),
        "relief25": ndi.maximum_filter(filled, 51) - ndi.minimum_filter(filled, 51),
    }
    for name, f in feats.items():
        rec[f"auc_{name}"] = _auc(f[pos_m], f[neg_m])
    return rec


def drainage_diag_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-drainage-diag",
        description="Measure descent-track alignment with the D8 network per mosaic.",
    )
    p.add_argument("manifest", type=Path)
    p.add_argument("out_json", type=Path)
    p.add_argument("--root", type=Path, help="Base dir for manifest paths (default: manifest dir).")
    p.add_argument("--max-pixels", type=float, default=60e6, help="Skip larger mosaics.")
    a = p.parse_args(argv)

    root = a.root or a.manifest.parent
    out = []
    for entry in json.loads(a.manifest.read_text()):
        dem = root / entry["dem_path"]
        with rasterio.open(dem) as src:
            if src.width * src.height > a.max_pixels:
                log(f"drainage-diag: skipping {dem.name} (> max-pixels)")
                continue
        kmls = [root / k for k in entry.get("kml_paths", [entry.get("kml_path")]) if k]
        rec = diagnose_mosaic(dem, kmls)
        print(json.dumps(rec), flush=True)
        out.append(rec)
    a.out_json.write_text(json.dumps(out, indent=1))

    keys = [k for k in out[0] if k not in {"name", "route_px"} and not k.startswith("stream_px")]
    for k in keys if out else []:
        v = np.array([r[k] for r in out if k in r], dtype=float)
        print(f"{k:26s} median={np.nanmedian(v):.3f} min={np.nanmin(v):.3f} max={np.nanmax(v):.3f}")


if __name__ == "__main__":
    drainage_diag_cli()

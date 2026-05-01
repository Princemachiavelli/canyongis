"""Sample geomorphon ternary + forms along Kirk Creek descent and rank rappel candidates."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from shapely.geometry import LineString

from canyongis.validate import parse_kml

KML = Path("data/ropewiki/kml/Kirk_Creek_Canyon.kml")
GEO_DIR = Path("tmp/kirk-geomorph-3m8m")
DEM = Path("data/ropewiki/dem/mosaic/Kirk_Creek_Canyon_buffer1200m.tif")
OUT = Path("tmp/kirk-rappel-candidates.json")

FORMS = {
    1: "flat", 2: "peak", 3: "ridge", 4: "shoulder", 5: "spur",
    6: "slope", 7: "hollow", 8: "footslope", 9: "valley", 10: "pit",
}


def sample_line(line: LineString, step_m: float = 1.0) -> np.ndarray:
    distances = np.arange(0.0, line.length, step_m)
    return np.array([(p.x, p.y) for p in (line.interpolate(d) for d in distances)]), distances


def read_full(path: Path) -> tuple[np.ndarray, "rasterio.Affine"]:
    with rasterio.open(path) as src:
        return src.read(1), src.transform


def window_pick(
    arr: np.ndarray,
    transform,
    xs: np.ndarray,
    ys: np.ndarray,
    radius_m: float,
    *,
    reduce: str = "max",
) -> np.ndarray:
    rows, cols = rasterio.transform.rowcol(transform, xs, ys)
    rows = np.asarray(rows)
    cols = np.asarray(cols)
    r = int(round(radius_m))
    h, w = arr.shape
    out = np.empty(len(rows), dtype=arr.dtype if reduce != "mean" else np.float32)
    for i, (rr, cc) in enumerate(zip(rows, cols)):
        r0, r1 = max(0, rr - r), min(h, rr + r + 1)
        c0, c1 = max(0, cc - r), min(w, cc + r + 1)
        win = arr[r0:r1, c0:c1]
        if reduce == "max":
            out[i] = win.max()
        elif reduce == "min":
            out[i] = win.min()
        elif reduce == "any_shoulder":
            out[i] = int(np.any(win == 4))
        else:
            out[i] = win.mean()
    return out


def main() -> None:
    feats = parse_kml(KML)
    with rasterio.open(DEM) as dem:
        line = feats.descent_lines.to_crs(dem.crs).geometry.union_all()
    pts, dists = sample_line(line, step_m=1.0)
    xs, ys = pts[:, 0], pts[:, 1]

    forms_arr, tr = read_full(GEO_DIR / "geomorphon_forms.tif")
    pos_arr, _ = read_full(GEO_DIR / "geomorphon_positive.tif")
    neg_arr, _ = read_full(GEO_DIR / "geomorphon_negative.tif")
    int_arr, _ = read_full(GEO_DIR / "geomorphon_intensity.tif")
    tern_arr, _ = read_full(GEO_DIR / "geomorphon_ternary.tif")

    # ±3m perpendicular buffer is approximated by a 3m square window around each line point.
    BUF = 3.0
    positive = window_pick(pos_arr, tr, xs, ys, BUF, reduce="max")
    negative = window_pick(neg_arr, tr, xs, ys, BUF, reduce="min")  # min negative = strongest down-look
    intensity = window_pick(int_arr.astype(np.float32), tr, xs, ys, BUF, reduce="max")
    has_shoulder = window_pick(forms_arr, tr, xs, ys, BUF, reduce="any_shoulder")
    forms = window_pick(forms_arr, tr, xs, ys, 0, reduce="max")  # exact pixel form
    ternary = window_pick(tern_arr, tr, xs, ys, 0, reduce="max")

    # Forms histogram along the descent
    vals, counts = np.unique(forms, return_counts=True)
    hist = {FORMS.get(int(v), str(int(v))): int(c) for v, c in zip(vals, counts)}
    print("forms along descent:", hist)

    # Rappel candidates: cluster contiguous shoulder (4) / spur (5) / ridge (3) runs
    # plus large positive-negative asymmetry (zenith-heavy = cliff lip).
    # asymmetry: max positives in window minus min negatives (strongest zenith vs nadir).
    asym = positive.astype(np.int32) - negative.astype(np.int32)
    is_lip = (has_shoulder.astype(bool)) | (asym >= 4) | (intensity >= 15.0)

    # Group contiguous True runs along the line into single candidates.
    candidates = []
    in_run = False
    start = 0
    for i, v in enumerate(is_lip):
        if v and not in_run:
            in_run = True
            start = i
        elif not v and in_run:
            in_run = False
            candidates.append((start, i - 1))
    if in_run:
        candidates.append((start, len(is_lip) - 1))

    # Filter: require run length >= 2m AND merge candidates within 5m of each other.
    merged: list[tuple[int, int]] = []
    for s, e in candidates:
        if merged and s - merged[-1][1] <= 5:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    merged = [(s, e) for s, e in merged if (e - s) >= 0]

    # Score each candidate by mean asymmetry + ternary diversity in window.
    out = []
    for s, e in merged:
        seg = slice(max(0, s - 1), min(len(is_lip), e + 2))
        score = float(asym[seg].mean())
        out.append({
            "along_m_start": float(dists[s]),
            "along_m_end": float(dists[e]),
            "lon_start_lat_lat_end": [
                float(pts[s, 0]), float(pts[s, 1]),
                float(pts[e, 0]), float(pts[e, 1]),
            ],
            "forms": [FORMS.get(int(v), str(int(v))) for v in forms[s:e + 1]],
            "ternary_codes": [int(v) for v in ternary[s:e + 1]],
            "mean_asym_pos_minus_neg": score,
            "max_intensity_deg": float(intensity[s:e + 1].max()),
        })

    out.sort(key=lambda r: -r["mean_asym_pos_minus_neg"])
    OUT.write_text(json.dumps({"forms_histogram": hist, "candidates": out}, indent=2))
    print(f"wrote {len(out)} rappel candidates → {OUT}")
    print(f"top 12 by asymmetry score:")
    for r in out[:12]:
        print(
            f"  along {r['along_m_start']:6.0f}–{r['along_m_end']:6.0f} m  "
            f"asym={r['mean_asym_pos_minus_neg']:+.1f}  "
            f"intensity={r['max_intensity_deg']:.1f}°  "
            f"forms={r['forms'][:6]}"
        )


if __name__ == "__main__":
    main()

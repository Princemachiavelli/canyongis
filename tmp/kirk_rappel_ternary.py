"""Find drop-peak rappel candidates along Kirk descent, dump ternary signatures."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from scipy.signal import find_peaks

from canyongis.validate import parse_kml

KML = Path("data/ropewiki/kml/Kirk_Creek_Canyon.kml")
DEM = Path("data/ropewiki/dem/mosaic/Kirk_Creek_Canyon_buffer1200m.tif")
GEO = Path("tmp/kirk-geomorph-3m8m")
DROP5 = Path("tmp/kirk-previews/Kirk_Creek_Canyon_drop_to_lower_ground_5m.tif")
DROP2 = Path("tmp/kirk-previews/Kirk_Creek_Canyon_drop_to_lower_ground_2m.tif")
OUT = Path("tmp/kirk-rappel-ternary.json")

FORMS = {
    1: "flat",
    2: "peak",
    3: "ridge",
    4: "shoulder",
    5: "spur",
    6: "slope",
    7: "hollow",
    8: "footslope",
    9: "valley",
    10: "pit",
    255: "nodata",
}


def main() -> None:
    feats = parse_kml(KML)
    with rasterio.open(DEM) as dem:
        line = feats.descent_lines.to_crs(dem.crs).geometry.union_all()

    step = 0.5
    dists = np.arange(0.0, line.length, step)
    pts = np.array([(p.x, p.y) for p in (line.interpolate(d) for d in dists)])
    xs, ys = pts[:, 0], pts[:, 1]

    rasters = {
        n: rasterio.open(p)
        for n, p in [
            ("drop2", DROP2),
            ("drop5", DROP5),
            ("forms", GEO / "geomorphon_forms.tif"),
            ("ternary", GEO / "geomorphon_ternary.tif"),
            ("pos", GEO / "geomorphon_positive.tif"),
            ("neg", GEO / "geomorphon_negative.tif"),
            ("intensity", GEO / "geomorphon_intensity.tif"),
        ]
    }
    arrs = {n: r.read(1) for n, r in rasters.items()}
    tr = next(iter(rasters.values())).transform

    rows, cols = rasterio.transform.rowcol(tr, xs, ys)
    rows = np.clip(np.asarray(rows), 0, arrs["drop5"].shape[0] - 1)
    cols = np.clip(np.asarray(cols), 0, arrs["drop5"].shape[1] - 1)

    # Buffer ±3m: take max drop within a 3-pixel window around each sample.
    R = 3
    h, w = arrs["drop5"].shape
    drop5_buf = np.empty(len(rows), dtype=np.float32)
    for i, (rr, cc) in enumerate(zip(rows, cols)):
        win = arrs["drop5"][
            max(0, rr - R) : min(h, rr + R + 1), max(0, cc - R) : min(w, cc + R + 1)
        ]
        win = win[np.isfinite(win)]
        drop5_buf[i] = win.max() if win.size else 0.0

    # Peak-find along descent. Min height = 2m drop, min separation = 6m (12 samples at 0.5m).
    peaks, props = find_peaks(drop5_buf, height=2.0, distance=12)

    # For each peak, locate the actual max-drop pixel within the buffer and read its codes.
    candidates = []
    for pk in peaks:
        rr, cc = rows[pk], cols[pk]
        r0, r1 = max(0, rr - R), min(h, rr + R + 1)
        c0, c1 = max(0, cc - R), min(w, cc + R + 1)
        win = arrs["drop5"][r0:r1, c0:c1]
        finite_win = np.where(np.isfinite(win), win, -np.inf)
        local = np.unravel_index(np.argmax(finite_win), win.shape)
        pr, pc = r0 + local[0], c0 + local[1]
        candidates.append(
            {
                "along_m": float(dists[pk]),
                "x": float(rasterio.transform.xy(tr, pr, pc)[0]),
                "y": float(rasterio.transform.xy(tr, pr, pc)[1]),
                "drop_2m": float(arrs["drop2"][pr, pc]),
                "drop_5m": float(arrs["drop5"][pr, pc]),
                "form": FORMS.get(int(arrs["forms"][pr, pc]), "?"),
                "ternary_code": int(arrs["ternary"][pr, pc]),
                "positive": int(arrs["pos"][pr, pc]),
                "negative": int(arrs["neg"][pr, pc]),
                "intensity_deg": float(arrs["intensity"][pr, pc]),
            }
        )

    OUT.write_text(json.dumps(candidates, indent=2))
    print(f"found {len(candidates)} drop-peak candidates along {line.length:.0f}m descent")
    print(
        f"{'along_m':>8} {'drop2':>6} {'drop5':>6} {'form':<10} {'ternary':>8} {'+':>3} {'-':>3} {'intens':>7}"
    )
    for c in candidates:
        print(
            f"{c['along_m']:8.1f} {c['drop_2m']:6.1f} {c['drop_5m']:6.1f} {c['form']:<10} {c['ternary_code']:8d} {c['positive']:3d} {c['negative']:3d} {c['intensity_deg']:7.1f}"
        )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import html
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point
from skimage.graph import route_through_array

from .io import log
from .validate import parse_kml

SnapMode = Literal["min", "max"]


@dataclass
class KmlSnapResult:
    input_kml_path: str
    snap_raster_path: str
    snapped_kml_path: str
    snapped_gpkg_path: str
    snap_mode: str
    search_radius_m: float
    sample_step_m: float
    high_threshold: float | None
    feature_count: int
    mean_shift_m: float
    max_shift_m: float


@dataclass(frozen=True)
class SnapCandidate:
    x: float
    y: float
    value: float
    shift_m: float
    score: float


def _line_parts(geom) -> list[LineString]:
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []


def _densify_line(line: LineString, step_m: float) -> LineString:
    if line.length == 0 or step_m <= 0:
        return line
    count = max(2, int(math.ceil(line.length / step_m)) + 1)
    distances = np.linspace(0.0, line.length, count)
    return LineString([line.interpolate(float(distance)).coords[0] for distance in distances])


def _snap_candidates(
    x: float,
    y: float,
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    radius_px: int,
    mode: SnapMode,
    score_lo: float,
    score_hi: float,
    candidate_count: int,
) -> list[SnapCandidate]:
    row, col = src.index(x, y)
    height, width = values.shape
    r0 = max(0, row - radius_px)
    r1 = min(height, row + radius_px + 1)
    c0 = max(0, col - radius_px)
    c1 = min(width, col + radius_px + 1)
    if r0 >= r1 or c0 >= c1:
        return [SnapCandidate(x, y, math.nan, 0.0, -math.inf)]

    window = values[r0:r1, c0:c1]
    rows, cols = np.ogrid[r0:r1, c0:c1]
    radius_mask = (rows - row) ** 2 + (cols - col) ** 2 <= radius_px**2
    valid = np.isfinite(window) & radius_mask
    if not np.any(valid):
        return [SnapCandidate(x, y, math.nan, 0.0, -math.inf)]

    span = score_hi - score_lo
    if span <= 0:
        span = 1.0
    score_window = np.full(window.shape, -np.inf, dtype=np.float32)
    if mode == "min":
        score_window[valid] = np.clip((score_hi - window[valid]) / span, 0.0, 1.0)
    else:
        score_window[valid] = np.clip((window[valid] - score_lo) / span, 0.0, 1.0)

    flat_scores = score_window.ravel()
    valid_count = int(np.isfinite(flat_scores).sum())
    pool_size = min(valid_count, max(candidate_count * 80, candidate_count))
    if pool_size <= 0:
        return [SnapCandidate(x, y, math.nan, 0.0, -math.inf)]
    candidate_flats = np.argpartition(flat_scores, -pool_size)[-pool_size:]
    candidate_flats = candidate_flats[np.argsort(flat_scores[candidate_flats])[::-1]]

    candidates = []
    min_sep_px = max(1, radius_px // 8)
    chosen_cells: list[tuple[int, int]] = []
    for flat in candidate_flats:
        if len(candidates) >= candidate_count:
            break
        wr, wc = np.unravel_index(int(flat), window.shape)
        snapped_row = r0 + int(wr)
        snapped_col = c0 + int(wc)
        if any(
            (snapped_row - prev_row) ** 2 + (snapped_col - prev_col) ** 2 < min_sep_px**2
            for prev_row, prev_col in chosen_cells
        ):
            continue
        snapped_x, snapped_y = src.xy(snapped_row, snapped_col)
        shift_m = math.hypot(float(snapped_x) - x, float(snapped_y) - y)
        candidates.append(
            SnapCandidate(
                float(snapped_x),
                float(snapped_y),
                float(values[snapped_row, snapped_col]),
                shift_m,
                float(flat_scores[flat]),
            )
        )
        chosen_cells.append((snapped_row, snapped_col))
    return candidates


def _snap_xy(
    x: float,
    y: float,
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    radius_px: int,
    mode: SnapMode,
) -> tuple[float, float, float, float]:
    row, col = src.index(x, y)
    height, width = values.shape
    r0 = max(0, row - radius_px)
    r1 = min(height, row + radius_px + 1)
    c0 = max(0, col - radius_px)
    c1 = min(width, col + radius_px + 1)
    if r0 >= r1 or c0 >= c1:
        return x, y, math.nan, 0.0

    window = values[r0:r1, c0:c1]
    rows, cols = np.ogrid[r0:r1, c0:c1]
    radius_mask = (rows - row) ** 2 + (cols - col) ** 2 <= radius_px**2
    valid = np.isfinite(window) & radius_mask
    if not np.any(valid):
        return x, y, math.nan, 0.0

    scores = np.where(valid, window, np.inf if mode == "min" else -np.inf)
    flat = int(np.nanargmin(scores) if mode == "min" else np.nanargmax(scores))
    wr, wc = np.unravel_index(flat, window.shape)
    snapped_row = r0 + int(wr)
    snapped_col = c0 + int(wc)
    snapped_x, snapped_y = src.xy(snapped_row, snapped_col)
    shift_m = math.hypot(float(snapped_x) - x, float(snapped_y) - y)
    return float(snapped_x), float(snapped_y), float(values[snapped_row, snapped_col]), shift_m


def _snap_xy_high_region_center(
    x: float,
    y: float,
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    radius_px: int,
    high_threshold: float | None = None,
) -> tuple[float, float, float, float]:
    row, col = src.index(x, y)
    height, width = values.shape
    r0 = max(0, row - radius_px)
    r1 = min(height, row + radius_px + 1)
    c0 = max(0, col - radius_px)
    c1 = min(width, col + radius_px + 1)
    if r0 >= r1 or c0 >= c1:
        return x, y, math.nan, 0.0

    window = values[r0:r1, c0:c1]
    rows, cols = np.ogrid[r0:r1, c0:c1]
    radius_mask = (rows - row) ** 2 + (cols - col) ** 2 <= radius_px**2
    valid = np.isfinite(window) & radius_mask
    if not np.any(valid):
        return x, y, math.nan, 0.0

    valid_values = window[valid]
    local_max = float(np.nanmax(valid_values))
    threshold = (
        high_threshold
        if high_threshold is not None and high_threshold <= local_max
        else max(float(np.nanpercentile(valid_values, 96)), local_max * 0.92)
    )
    high = valid & (window >= threshold)
    if not np.any(high):
        return _snap_xy(x, y, values, src, radius_px, "max")

    labels, label_count = ndi.label(high)
    if label_count > 0:
        best_label = 0
        best_value = -math.inf
        for label in range(1, label_count + 1):
            label_values = window[labels == label]
            label_max = float(np.nanmax(label_values))
            if label_max > best_value:
                best_value = label_max
                best_label = label
        high = labels == best_label

    medial = ndi.distance_transform_edt(high)
    value_tiebreak = np.clip(window / max(local_max, 1e-6), 0.0, 1.0) * 0.01
    score = np.where(high, medial + value_tiebreak, -np.inf)
    wr, wc = np.unravel_index(int(np.nanargmax(score)), score.shape)
    snapped_row = r0 + int(wr)
    snapped_col = c0 + int(wc)
    snapped_x, snapped_y = src.xy(snapped_row, snapped_col)
    shift_m = math.hypot(float(snapped_x) - x, float(snapped_y) - y)
    return float(snapped_x), float(snapped_y), float(values[snapped_row, snapped_col]), shift_m


def _route_between_high_cells(
    coords: list[tuple[float, float]],
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    high_threshold: float | None,
    radius_px: int,
) -> list[tuple[float, float]]:
    if len(coords) < 2:
        return coords

    routed: list[tuple[float, float]] = []
    pixel_m = max(abs(src.transform.a), abs(src.transform.e))
    finite = values[np.isfinite(values)]
    fallback_hi = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
    threshold = high_threshold if high_threshold is not None else fallback_hi
    scale = max(abs(threshold), 1e-6)

    for start_xy, end_xy in zip(coords[:-1], coords[1:]):
        sr, sc = src.index(*start_xy)
        er, ec = src.index(*end_xy)
        pad = max(radius_px, int(math.ceil(math.hypot(er - sr, ec - sc))) // 2 + 2)
        r0 = max(0, min(sr, er) - pad)
        r1 = min(values.shape[0], max(sr, er) + pad + 1)
        c0 = max(0, min(sc, ec) - pad)
        c1 = min(values.shape[1], max(sc, ec) + pad + 1)
        if r0 >= r1 or c0 >= c1:
            continue

        window = values[r0:r1, c0:c1]
        finite_window = np.isfinite(window)
        if not np.any(finite_window):
            continue

        deficit = np.maximum(threshold - window, 0.0) / scale
        cost = 1.0 + np.square(deficit) * 5000.0
        cost = np.where(finite_window, cost, 1e6).astype(np.float32)
        start = (sr - r0, sc - c0)
        end = (er - r0, ec - c0)
        try:
            path, _ = route_through_array(
                cost,
                start,
                end,
                fully_connected=True,
                geometric=True,
            )
        except Exception:
            segment = [start_xy, end_xy]
        else:
            segment = [src.xy(r0 + int(row), c0 + int(col)) for row, col in path]

        for x, y in segment:
            coord = (float(x), float(y))
            if (
                not routed
                or math.hypot(coord[0] - routed[-1][0], coord[1] - routed[-1][1]) >= pixel_m * 0.5
            ):
                routed.append(coord)

    return routed or coords


def _snap_point(
    point: Point,
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    radius_px: int,
    mode: SnapMode,
) -> tuple[Point, float, float]:
    if mode == "max":
        x, y, snap_value, shift = _snap_xy_high_region_center(
            point.x, point.y, values, src, radius_px
        )
    else:
        x, y, snap_value, shift = _snap_xy(point.x, point.y, values, src, radius_px, mode)
    return Point(x, y), snap_value, shift


def _snap_line(
    line: LineString,
    values: np.ndarray,
    src: rasterio.io.DatasetReader,
    radius_px: int,
    mode: SnapMode,
    sample_step_m: float,
    high_threshold: float | None,
) -> tuple[LineString, float, float, float]:
    dense = _densify_line(line, sample_step_m)
    if mode == "max":
        coords = []
        snap_values = []
        shifts = []
        last: tuple[float, float] | None = None
        for x, y in dense.coords:
            sx, sy, snap_value, shift = _snap_xy_high_region_center(
                x, y, values, src, radius_px, high_threshold
            )
            coord = (sx, sy)
            if coord != last:
                coords.append(coord)
                last = coord
            if math.isfinite(snap_value):
                snap_values.append(snap_value)
            shifts.append(shift)
        if len(coords) < 2:
            coords = list(dense.coords)
        coords = _route_between_high_cells(coords, values, src, high_threshold, radius_px)
        return (
            LineString(coords),
            float(np.nanmean(snap_values)) if snap_values else math.nan,
            float(np.nanmean(shifts)) if shifts else 0.0,
            float(np.nanmax(shifts)) if shifts else 0.0,
        )

    sample_coords = list(dense.coords)
    finite_values = values[np.isfinite(values)]
    if finite_values.size == 0:
        return dense, math.nan, 0.0, 0.0
    score_lo, score_hi = (float(v) for v in np.nanpercentile(finite_values, [1, 99]))
    if score_hi <= score_lo:
        score_lo = float(np.nanmin(finite_values))
        score_hi = float(np.nanmax(finite_values))

    candidate_sets = [
        _snap_candidates(
            x,
            y,
            values,
            src,
            radius_px,
            mode,
            score_lo,
            score_hi,
            candidate_count=48,
        )
        for x, y in sample_coords
    ]
    if not candidate_sets:
        return dense, math.nan, 0.0, 0.0

    radius_m = max(radius_px * max(abs(src.transform.a), abs(src.transform.e)), 1.0)
    distance_weight = 0.15
    continuity_weight = 0.65
    dp = []
    backrefs: list[np.ndarray] = []
    first = np.array(
        [
            candidate.score - distance_weight * (candidate.shift_m / radius_m)
            for candidate in candidate_sets[0]
        ],
        dtype=np.float64,
    )
    dp.append(first)
    for i in range(1, len(candidate_sets)):
        prev_candidates = candidate_sets[i - 1]
        candidates = candidate_sets[i]
        scores = np.full(len(candidates), -np.inf, dtype=np.float64)
        back = np.zeros(len(candidates), dtype=np.int32)
        x0, y0 = sample_coords[i - 1]
        x1, y1 = sample_coords[i]
        expected = math.hypot(x1 - x0, y1 - y0)
        expected = max(expected, sample_step_m)
        for j, candidate in enumerate(candidates):
            best_score = -math.inf
            best_idx = 0
            for k, prev in enumerate(prev_candidates):
                jump = math.hypot(candidate.x - prev.x, candidate.y - prev.y)
                continuity = abs(jump - expected) / max(expected, 1.0)
                total = (
                    dp[-1][k]
                    + candidate.score
                    - distance_weight * (candidate.shift_m / radius_m)
                    - continuity_weight * continuity
                )
                if total > best_score:
                    best_score = total
                    best_idx = k
            scores[j] = best_score
            back[j] = best_idx
        dp.append(scores)
        backrefs.append(back)

    idx = int(np.nanargmax(dp[-1]))
    chosen = [candidate_sets[-1][idx]]
    for i in range(len(backrefs) - 1, -1, -1):
        idx = int(backrefs[i][idx])
        chosen.append(candidate_sets[i][idx])
    chosen.reverse()

    coords = []
    snap_values = []
    shifts = []
    last: tuple[float, float] | None = None
    for candidate in chosen:
        coord = (candidate.x, candidate.y)
        if coord != last:
            coords.append(coord)
            last = coord
        if math.isfinite(candidate.value):
            snap_values.append(candidate.value)
        shifts.append(candidate.shift_m)
    if len(coords) < 2:
        coords = list(dense.coords)
    return (
        LineString(coords),
        float(np.nanmean(snap_values)) if snap_values else math.nan,
        float(np.nanmean(shifts)) if shifts else 0.0,
        float(np.nanmax(shifts)) if shifts else 0.0,
    )


def _feature_rows(features) -> gpd.GeoDataFrame:
    frames = []
    descent_lines = features.descent_lines
    if not descent_lines.empty:
        descent_names = descent_lines["name"].astype(str).str.lower()
        explicit_descent = descent_lines[descent_names.str.contains("descent", na=False)]
        if not explicit_descent.empty:
            descent_lines = explicit_descent.reset_index(drop=True)
    for fallback_category, gdf in (("descent", descent_lines),):
        if gdf.empty:
            continue
        frame = gdf.copy()
        if "category" not in frame.columns:
            frame["category"] = fallback_category
        frame["source_category"] = fallback_category
        frames.append(frame)
    if not frames:
        return gpd.GeoDataFrame(
            columns=["name", "category", "geometry"],
            geometry="geometry",
            crs="EPSG:4326",
        )
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326"
    )


def _style_for_category(category: str) -> str:
    if category == "descent":
        return "descentStyle"
    if category == "approach":
        return "approachStyle"
    if category == "point":
        return "pointStyle"
    return "unknownStyle"


def _write_snapped_kml(gdf: gpd.GeoDataFrame, path: Path) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        '<Style id="descentStyle"><LineStyle><color>ff2f3fd3</color>'
        "<width>5</width></LineStyle></Style>",
        '<Style id="approachStyle"><LineStyle><color>ffeb6325</color>'
        "<width>4</width></LineStyle></Style>",
        '<Style id="unknownStyle"><LineStyle><color>ff6d6559</color>'
        "<width>4</width></LineStyle></Style>",
        '<Style id="pointStyle"><IconStyle><color>ff2eb8f5</color>'
        "<scale>0.8</scale></IconStyle></Style>",
    ]
    for idx, (_, row) in enumerate(gdf.to_crs("EPSG:4326").iterrows()):
        geom = row.geometry
        category = str(row.get("category") or row.get("source_category") or "unknown")
        name = html.escape(str(row.get("name") or f"snapped {idx + 1}"))
        description = html.escape(
            f"snapped by canyongis; mean shift {float(row.get('mean_shift_m', 0.0)):.1f} m; "
            f"max shift {float(row.get('max_shift_m', 0.0)):.1f} m"
        )
        style = _style_for_category(category)
        for part in _line_parts(geom):
            coords = " ".join(f"{x},{y},0" for x, y in part.coords)
            lines.append(
                f"<Placemark><name>{name}</name><description>{description}</description>"
                f"<styleUrl>#{style}</styleUrl><LineString><tessellate>1</tessellate>"
                f"<coordinates>{coords}</coordinates></LineString></Placemark>"
            )
        if geom.geom_type == "Point":
            lines.append(
                f"<Placemark><name>{name}</name><description>{description}</description>"
                f"<styleUrl>#pointStyle</styleUrl>"
                f"<Point><coordinates>{geom.x},{geom.y},0</coordinates></Point></Placemark>"
            )
    lines.append("</Document></kml>")
    path.write_text("\n".join(lines), encoding="utf-8")


def snap_kml_to_raster(
    kml_path: str | Path,
    raster_path: str | Path,
    out_dir: str | Path,
    *,
    snap_mode: SnapMode = "min",
    search_radius_m: float = 30.0,
    sample_step_m: float = 10.0,
    high_threshold: float | None = None,
    output_name: str = "snapped.kml",
) -> KmlSnapResult:
    if search_radius_m <= 0:
        raise ValueError("search_radius_m must be positive")
    if sample_step_m <= 0:
        raise ValueError("sample_step_m must be positive")

    kml_path = Path(kml_path)
    raster_path = Path(raster_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    features = parse_kml(kml_path)
    rows = _feature_rows(features)
    if rows.empty:
        raise ValueError(f"KML contains no descent LineString geometry: {kml_path}")

    with rasterio.open(raster_path) as src:
        values = src.read(1).astype(np.float32)
        if src.nodata is not None:
            values[values == src.nodata] = np.nan
        pixel_m = max(abs(src.transform.a), abs(src.transform.e))
        radius_px = max(1, int(math.ceil(search_radius_m / pixel_m)))
        projected = rows.to_crs(src.crs)
        snapped_rows = []
        all_shifts: list[float] = []
        for _, row in projected.iterrows():
            geom = row.geometry
            base = row.drop(labels=["geometry"]).to_dict()
            if geom.geom_type in {"LineString", "MultiLineString"}:
                for part in _line_parts(geom):
                    snapped, mean_value, mean_shift, max_shift = _snap_line(
                        part, values, src, radius_px, snap_mode, sample_step_m, high_threshold
                    )
                    out = {
                        **base,
                        "geometry": snapped,
                        "original_length_m": float(part.length),
                        "snapped_length_m": float(snapped.length),
                        "mean_snap_value": mean_value,
                        "mean_shift_m": mean_shift,
                        "max_shift_m": max_shift,
                    }
                    snapped_rows.append(out)
                    all_shifts.append(mean_shift)
            elif geom.geom_type == "Point":
                snapped, snap_value, shift = _snap_point(geom, values, src, radius_px, snap_mode)
                snapped_rows.append(
                    {
                        **base,
                        "geometry": snapped,
                        "mean_snap_value": snap_value,
                        "mean_shift_m": shift,
                        "max_shift_m": shift,
                    }
                )
                all_shifts.append(shift)

        snapped_gdf = gpd.GeoDataFrame(snapped_rows, geometry="geometry", crs=src.crs).to_crs(
            "EPSG:4326"
        )

    snapped_kml_path = out_dir / output_name
    snapped_gpkg_path = snapped_kml_path.with_suffix(".gpkg")
    snapped_gdf.to_file(snapped_gpkg_path, driver="GPKG")
    _write_snapped_kml(snapped_gdf, snapped_kml_path)
    return KmlSnapResult(
        input_kml_path=str(kml_path),
        snap_raster_path=str(raster_path),
        snapped_kml_path=str(snapped_kml_path),
        snapped_gpkg_path=str(snapped_gpkg_path),
        snap_mode=snap_mode,
        search_radius_m=search_radius_m,
        sample_step_m=sample_step_m,
        high_threshold=high_threshold,
        feature_count=len(snapped_gdf),
        mean_shift_m=float(np.nanmean(all_shifts)) if all_shifts else 0.0,
        max_shift_m=float(np.nanmax(all_shifts)) if all_shifts else 0.0,
    )


def kml_snap_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-kml-snap",
        description="Snap KML LineString/Point features to nearby DEM lows or raster-score highs.",
    )
    p.add_argument("kml", type=Path, help="Input KML path.")
    p.add_argument("raster", type=Path, help="DEM or ravine-score raster path.")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument(
        "--snap-mode",
        choices=("min", "max"),
        default="min",
        help="Use min for DEM low points, max for TPI/geomorphon/curvature score rasters.",
    )
    p.add_argument("--search-radius-m", type=float, default=30.0)
    p.add_argument("--sample-step-m", type=float, default=10.0)
    p.add_argument(
        "--high-threshold",
        type=float,
        default=None,
        help="For --snap-mode=max, snap to the center of pixels at or above this raster value.",
    )
    p.add_argument("--output-name", default="snapped.kml")
    args = p.parse_args(argv)

    result = snap_kml_to_raster(
        args.kml,
        args.raster,
        args.outdir,
        snap_mode=args.snap_mode,
        search_radius_m=args.search_radius_m,
        sample_step_m=args.sample_step_m,
        high_threshold=args.high_threshold,
        output_name=args.output_name,
    )
    log(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    kml_snap_cli()

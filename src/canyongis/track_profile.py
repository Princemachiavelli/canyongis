from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.warp import transform as transform_coords
from shapely.geometry import LineString, MultiLineString, Point

from .validate import parse_kml

TrackCategory = Literal["descent", "approach", "unclassified"]


@dataclass
class ProfileSample:
    distance_m: float
    elevation_m: float | None
    slope: float | None
    lon: float
    lat: float


@dataclass
class RappelCandidate:
    distance_m: float
    elevation_m: float | None
    drop_m: float
    length_m: float
    slope: float
    lon: float
    lat: float


@dataclass
class TrackProfileResult:
    kml_path: str
    dem_path: str
    category: str
    track_index: int
    track_name: str
    length_m: float
    total_drop_m: float | None
    min_elevation_m: float | None
    max_elevation_m: float | None
    sample_step_m: float
    rappel_window_m: float
    min_rappel_drop_m: float
    min_rappel_slope: float
    samples: list[ProfileSample]
    rappel_candidates: list[RappelCandidate]


def _category_lines(features, category: TrackCategory) -> gpd.GeoDataFrame:
    if category == "descent":
        return features.descent_lines
    if category == "approach":
        return features.approach_lines
    return features.unclassified_lines


def _single_line(geom) -> LineString:
    if isinstance(geom, LineString):
        return geom
    if isinstance(geom, MultiLineString):
        parts = [part for part in geom.geoms if part.length > 0]
        if not parts:
            raise ValueError("track geometry is empty")
        return max(parts, key=lambda part: part.length)
    raise ValueError(f"track geometry must be a LineString, got {geom.geom_type}")


def _sample_distances(length_m: float, step_m: float) -> np.ndarray:
    if length_m <= 0:
        return np.asarray([0.0], dtype=np.float64)
    count = max(2, int(math.ceil(length_m / step_m)) + 1)
    values = np.linspace(0.0, length_m, count, dtype=np.float64)
    values[-1] = length_m
    return values


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _elevation_samples(src, points: list[Point]) -> list[float]:
    nodata = src.nodata
    coords = [(point.x, point.y) for point in points]
    out: list[float] = []
    for row in src.sample(coords):
        value = float(row[0])
        if nodata is not None and value == nodata:
            out.append(math.nan)
        elif not math.isfinite(value):
            out.append(math.nan)
        else:
            out.append(value)
    return out


def _orient_downhill(
    line: LineString, distances: np.ndarray, points: list[Point], elevations: list[float]
) -> tuple[LineString, np.ndarray, list[Point], list[float]]:
    finite = [(idx, elev) for idx, elev in enumerate(elevations) if math.isfinite(elev)]
    if len(finite) < 2:
        return line, distances, points, elevations
    first_idx, first_elev = finite[0]
    last_idx, last_elev = finite[-1]
    if last_elev <= first_elev:
        return line, distances, points, elevations

    length = float(line.length)
    reversed_line = LineString(list(line.coords)[::-1])
    reversed_distances = length - distances[::-1]
    reversed_distances[0] = 0.0
    reversed_distances[-1] = length
    return reversed_line, reversed_distances, points[::-1], elevations[::-1]


def _slopes(distances: np.ndarray, elevations: list[float]) -> list[float | None]:
    slopes: list[float | None] = [None]
    for idx in range(1, len(distances)):
        run = float(distances[idx] - distances[idx - 1])
        z0 = elevations[idx - 1]
        z1 = elevations[idx]
        if run <= 0 or not math.isfinite(z0) or not math.isfinite(z1):
            slopes.append(None)
            continue
        slopes.append(float((z0 - z1) / run))
    return slopes


def _detect_rappels(
    distances: np.ndarray,
    elevations: list[float],
    points_wgs84: list[Point],
    *,
    window_m: float,
    min_drop_m: float,
    min_slope: float,
) -> list[RappelCandidate]:
    if len(distances) < 2:
        return []
    window_m = max(float(window_m), 0.1)
    candidates: list[tuple[int, int, float, float]] = []
    j = 0
    for i, start_m in enumerate(distances):
        while j < len(distances) - 1 and distances[j] - start_m < window_m:
            j += 1
        if j <= i or j >= len(distances):
            continue
        z0 = elevations[i]
        z1 = elevations[j]
        if not math.isfinite(z0) or not math.isfinite(z1):
            continue
        run = float(distances[j] - start_m)
        drop = float(z0 - z1)
        slope = drop / run if run > 0 else 0.0
        if drop >= min_drop_m and slope >= min_slope:
            candidates.append((i, j, drop, slope))

    if not candidates:
        return []

    groups: list[list[tuple[int, int, float, float]]] = []
    current: list[tuple[int, int, float, float]] = []
    prev_i = -99
    for candidate in candidates:
        if current and candidate[0] > prev_i + 1:
            groups.append(current)
            current = []
        current.append(candidate)
        prev_i = candidate[0]
    if current:
        groups.append(current)

    rappels: list[RappelCandidate] = []
    for group in groups:
        best = max(group, key=lambda item: (item[3], item[2]))
        start_i, end_i, drop, slope = best
        mid_distance = float((distances[start_i] + distances[end_i]) / 2.0)
        mid_i = int(np.argmin(np.abs(distances - mid_distance)))
        point = points_wgs84[mid_i]
        elevation = elevations[mid_i]
        rappels.append(
            RappelCandidate(
                distance_m=mid_distance,
                elevation_m=_finite_or_none(elevation),
                drop_m=float(drop),
                length_m=float(distances[end_i] - distances[start_i]),
                slope=float(slope),
                lon=float(point.x),
                lat=float(point.y),
            )
        )
    return rappels


def analyze_track_profile(
    kml_path: str | Path,
    dem_path: str | Path,
    *,
    category: TrackCategory = "descent",
    track_index: int = 0,
    sample_step_m: float = 5.0,
    rappel_window_m: float = 10.0,
    min_rappel_drop_m: float = 3.0,
    min_rappel_slope: float = 0.45,
) -> TrackProfileResult:
    kml_path = Path(kml_path)
    dem_path = Path(dem_path)
    features = parse_kml(kml_path)
    lines = _category_lines(features, category)
    if lines.empty:
        raise ValueError(f"KML has no {category} tracks: {kml_path}")
    if track_index < 0 or track_index >= len(lines):
        raise ValueError(f"{category} track index {track_index} out of range 0..{len(lines) - 1}")

    row = lines.iloc[int(track_index)]
    track_name = str(row.get("name") or f"{category} {track_index + 1}")
    line_wgs84 = _single_line(row.geometry)

    with rasterio.open(dem_path) as src:
        line_projected = gpd.GeoSeries([line_wgs84], crs="EPSG:4326").to_crs(src.crs).iloc[0]
        line_projected = _single_line(line_projected)
        distances = _sample_distances(float(line_projected.length), float(sample_step_m))
        points_projected = [line_projected.interpolate(float(distance)) for distance in distances]
        elevations = _elevation_samples(src, points_projected)
        line_projected, distances, points_projected, elevations = _orient_downhill(
            line_projected, distances, points_projected, elevations
        )
        xs = [point.x for point in points_projected]
        ys = [point.y for point in points_projected]
        lons, lats = transform_coords(src.crs, "EPSG:4326", xs, ys)
        points_wgs84 = [Point(lon, lat) for lon, lat in zip(lons, lats, strict=True)]

    slopes = _slopes(distances, elevations)
    samples = [
        ProfileSample(
            distance_m=float(distance),
            elevation_m=_finite_or_none(elevation),
            slope=slope,
            lon=float(point.x),
            lat=float(point.y),
        )
        for distance, elevation, slope, point in zip(distances, elevations, slopes, points_wgs84, strict=True)
    ]
    finite_elev = np.asarray([value for value in elevations if math.isfinite(value)], dtype=np.float64)
    total_drop = (
        float(finite_elev[0] - finite_elev[-1]) if finite_elev.size >= 2 else None
    )
    rappels = _detect_rappels(
        distances,
        elevations,
        points_wgs84,
        window_m=rappel_window_m,
        min_drop_m=min_rappel_drop_m,
        min_slope=min_rappel_slope,
    )
    return TrackProfileResult(
        kml_path=str(kml_path),
        dem_path=str(dem_path),
        category=category,
        track_index=int(track_index),
        track_name=track_name,
        length_m=float(line_projected.length),
        total_drop_m=total_drop,
        min_elevation_m=float(np.nanmin(finite_elev)) if finite_elev.size else None,
        max_elevation_m=float(np.nanmax(finite_elev)) if finite_elev.size else None,
        sample_step_m=float(sample_step_m),
        rappel_window_m=float(rappel_window_m),
        min_rappel_drop_m=float(min_rappel_drop_m),
        min_rappel_slope=float(min_rappel_slope),
        samples=samples,
        rappel_candidates=rappels,
    )


def _rappels_geojson(rappels: list[RappelCandidate]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": f"rappel candidate {idx + 1}",
                    "distance_m": rappel.distance_m,
                    "drop_m": rappel.drop_m,
                    "length_m": rappel.length_m,
                    "slope": rappel.slope,
                    "elevation_m": rappel.elevation_m,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [rappel.lon, rappel.lat],
                },
            }
            for idx, rappel in enumerate(rappels)
        ],
    }


def track_profile_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-track-profile",
        description="Sample a KML track against a DEM and flag steep rappel-like drops.",
    )
    p.add_argument("kml", type=Path, help="Input KML path.")
    p.add_argument("dem", type=Path, help="DEM GeoTIFF used for elevation samples.")
    p.add_argument("--category", choices=["descent", "approach", "unclassified"], default="descent")
    p.add_argument("--track-index", type=int, default=0)
    p.add_argument("--sample-step-m", type=float, default=5.0)
    p.add_argument("--rappel-window-m", type=float, default=10.0)
    p.add_argument("--min-rappel-drop-m", type=float, default=3.0)
    p.add_argument("--min-rappel-slope", type=float, default=0.45)
    p.add_argument("--rappels-geojson", type=Path, default=None)
    args = p.parse_args(argv)

    result = analyze_track_profile(
        args.kml,
        args.dem,
        category=args.category,
        track_index=args.track_index,
        sample_step_m=args.sample_step_m,
        rappel_window_m=args.rappel_window_m,
        min_rappel_drop_m=args.min_rappel_drop_m,
        min_rappel_slope=args.min_rappel_slope,
    )
    payload = asdict(result)
    payload["rappel_candidates_geojson"] = _rappels_geojson(result.rappel_candidates)
    print(json.dumps(payload, indent=2))
    if args.rappels_geojson:
        args.rappels_geojson.write_text(
            json.dumps(payload["rappel_candidates_geojson"], indent=2) + "\n"
        )


if __name__ == "__main__":
    track_profile_cli()

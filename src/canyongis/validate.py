"""Validate canyongis detection output against a ground-truth KML (e.g. CalTopo export).

Parses the KML into descent/approach categories, then scores detected layers in an
`out/` directory against those references. Reports counts, drop-height stats inside
the descent corridor, and over-detection outside it.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, MultiLineString, Point

KML_NS = "{http://www.opengis.net/kml/2.2}"

# Name substrings (case-insensitive) used to classify KML lines.
APPROACH_TOKENS = ("approach", "shortcut", "bushwack", "bushwhack", "exit", "trail")
DESCENT_TOKENS = ("descent", "rappel", "logjam", "canyon", "gorge", "creek")


@dataclass
class KmlFeatures:
    """Classified KML features in WGS84 (EPSG:4326)."""

    descent_lines: gpd.GeoDataFrame
    approach_lines: gpd.GeoDataFrame
    unclassified_lines: gpd.GeoDataFrame
    points: gpd.GeoDataFrame  # all points with name + category columns

    @property
    def all_descent(self) -> MultiLineString | LineString | None:
        if self.descent_lines.empty:
            return None
        geoms = list(self.descent_lines.geometry)
        return geoms[0] if len(geoms) == 1 else MultiLineString(geoms)


def _classify(name: str) -> str:
    n = name.lower()
    # Approach checks first — "Canyon Drop-in" should still classify as a canyon point,
    # but "Bramble Canyon Approach" must win "approach" over "canyon".
    if any(tok in n for tok in APPROACH_TOKENS):
        return "approach"
    if any(tok in n for tok in DESCENT_TOKENS):
        return "descent"
    return "unknown"


def _coord_tokens_to_line(text: str) -> LineString | None:
    pts = []
    for tok in text.split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if len(pts) < 2:
        return None
    return LineString(pts)


def _coord_tokens_to_point(text: str) -> Point | None:
    parts = text.strip().split(",")
    if len(parts) < 2:
        return None
    try:
        return Point(float(parts[0]), float(parts[1]))
    except ValueError:
        return None


def parse_kml(path: str | Path) -> KmlFeatures:
    """Parse a KML file into classified GeoDataFrames (WGS84)."""
    tree = ET.parse(str(path))
    root = tree.getroot()

    desc_lines: list[dict[str, Any]] = []
    appr_lines: list[dict[str, Any]] = []
    unk_lines: list[dict[str, Any]] = []
    pts: list[dict[str, Any]] = []

    for pm in root.iter(f"{KML_NS}Placemark"):
        name_el = pm.find(f"{KML_NS}name")
        name = (name_el.text or "").strip() if name_el is not None else ""
        category = _classify(name)

        for ls in pm.iter(f"{KML_NS}LineString"):
            coords = ls.find(f"{KML_NS}coordinates")
            if coords is None or not coords.text:
                continue
            geom = _coord_tokens_to_line(coords.text)
            if geom is None:
                continue
            row = {"name": name, "category": category, "geometry": geom}
            if category == "descent":
                desc_lines.append(row)
            elif category == "approach":
                appr_lines.append(row)
            else:
                unk_lines.append(row)

        for pt in pm.iter(f"{KML_NS}Point"):
            coords = pt.find(f"{KML_NS}coordinates")
            if coords is None or not coords.text:
                continue
            geom = _coord_tokens_to_point(coords.text)
            if geom is None:
                continue
            pts.append({"name": name, "category": category, "geometry": geom})

    wgs84 = "EPSG:4326"

    def _gdf(rows: list[dict[str, Any]]) -> gpd.GeoDataFrame:
        if not rows:
            return gpd.GeoDataFrame(
                columns=["name", "category", "geometry"], geometry="geometry", crs=wgs84
            )
        return gpd.GeoDataFrame(rows, crs=wgs84)

    return KmlFeatures(
        descent_lines=_gdf(desc_lines),
        approach_lines=_gdf(appr_lines),
        unclassified_lines=_gdf(unk_lines),
        points=_gdf(pts),
    )


# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------


@dataclass
class ValidationReport:
    kml_path: str
    out_dir: str
    buffer_m: float
    descent_names: list[str] = field(default_factory=list)
    descent_length_m: float = 0.0
    rappel_points: dict[str, Any] | None = None
    drainage_polys: dict[str, Any] | None = None
    candidate_routes: dict[str, Any] | None = None
    nearest_to_named_points: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kml_path": self.kml_path,
            "out_dir": self.out_dir,
            "buffer_m": self.buffer_m,
            "descent": {
                "names": self.descent_names,
                "length_m": self.descent_length_m,
            },
            "rappel_points": self.rappel_points,
            "drainage_polys": self.drainage_polys,
            "candidate_routes": self.candidate_routes,
            "nearest_to_named_points": self.nearest_to_named_points,
            "warnings": self.warnings,
        }


def _describe_heights(heights: np.ndarray) -> dict[str, float]:
    if heights.size == 0:
        return {"count": 0}
    return {
        "count": int(heights.size),
        "min": float(np.min(heights)),
        "median": float(np.median(heights)),
        "mean": float(np.mean(heights)),
        "p90": float(np.percentile(heights, 90)),
        "max": float(np.max(heights)),
    }


def _score_points(
    points_gpkg: Path, descent_buf_proj: Any, proj_crs: Any
) -> dict[str, Any]:
    pts = gpd.read_file(points_gpkg)
    if pts.crs is None:
        raise ValueError(f"{points_gpkg} has no CRS")
    pts_proj = pts.to_crs(proj_crs) if pts.crs != proj_crs else pts

    inside_mask = pts_proj.geometry.intersects(descent_buf_proj)
    inside = pts_proj[inside_mask]
    outside = pts_proj[~inside_mask]

    height_col = "drop_height_m" if "drop_height_m" in pts_proj.columns else None

    def _h(df):
        if height_col is None or df.empty:
            return np.array([], dtype=np.float32)
        return df[height_col].to_numpy(dtype=np.float32)

    return {
        "total": int(len(pts_proj)),
        "inside": {
            "count": int(len(inside)),
            "heights": _describe_heights(_h(inside)),
        },
        "outside": {
            "count": int(len(outside)),
            "heights": _describe_heights(_h(outside)),
        },
    }


def _score_polys(
    polys_gpkg: Path, descent_line_proj: Any, proj_crs: Any
) -> dict[str, Any]:
    polys = gpd.read_file(polys_gpkg)
    if polys.empty:
        return {"total": 0, "intersecting_descent": 0}
    polys_proj = polys.to_crs(proj_crs) if polys.crs != proj_crs else polys
    hits = polys_proj[polys_proj.geometry.intersects(descent_line_proj)]
    areas = polys_proj.geometry.area.to_numpy()
    return {
        "total": int(len(polys_proj)),
        "intersecting_descent": int(len(hits)),
        "area_m2": {
            "min": float(areas.min()) if areas.size else 0.0,
            "median": float(np.median(areas)) if areas.size else 0.0,
            "max": float(areas.max()) if areas.size else 0.0,
            "sum": float(areas.sum()) if areas.size else 0.0,
        },
    }


def _nearest_named_distances(
    named_pts_wgs84: gpd.GeoDataFrame,
    detected_pts_proj: gpd.GeoDataFrame,
    proj_crs: Any,
    names_of_interest: tuple[str, ...] = (
        "Canyon Drop-in",
        "Top of Gorge",
        "Bottom of Gorge",
        "Bramble Drop In",
        "Last Rappel",
    ),
) -> list[dict[str, Any]]:
    if named_pts_wgs84.empty or detected_pts_proj.empty:
        return []
    pts_proj = named_pts_wgs84.to_crs(proj_crs)
    detected_union = detected_pts_proj.geometry.union_all()
    rows = []
    for _, r in pts_proj.iterrows():
        if r["name"] not in names_of_interest:
            continue
        d = float(r.geometry.distance(detected_union))
        rows.append({"name": r["name"], "distance_to_nearest_detection_m": d})
    return rows


def validate_run(
    kml_path: str | Path,
    out_dir: str | Path,
    buffer_m: float = 25.0,
    descent_name: str | None = None,
) -> ValidationReport:
    """Compute validation metrics for a canyongis run in `out_dir` against `kml_path`."""
    out_dir = Path(out_dir)
    kml_path = Path(kml_path)

    features = parse_kml(kml_path)
    rep = ValidationReport(kml_path=str(kml_path), out_dir=str(out_dir), buffer_m=buffer_m)

    # Pick descent reference
    descents = features.descent_lines
    if descent_name is not None:
        descents = descents[descents["name"] == descent_name]
        if descents.empty:
            rep.warnings.append(
                f"descent_name={descent_name!r} not found; known descents: "
                f"{list(features.descent_lines['name'])}"
            )
            return rep
    if descents.empty:
        rep.warnings.append("no descent lines found in KML (name heuristics failed)")
        return rep

    points_gpkg = out_dir / "rappel_points.gpkg"
    if not points_gpkg.exists():
        rep.warnings.append(f"{points_gpkg} not found — cannot score rappel points")
        return rep

    # Use the detected points' CRS as our projected reference (meters-based assumed).
    ref = gpd.read_file(points_gpkg, rows=1)
    if ref.crs is None or not ref.crs.is_projected:
        rep.warnings.append(
            f"rappel_points CRS is not projected ({ref.crs}); metrics may be incorrect"
        )
    proj_crs = ref.crs

    descents_proj = descents.to_crs(proj_crs)
    rep.descent_names = list(descents_proj["name"])
    rep.descent_length_m = float(descents_proj.geometry.length.sum())

    descent_union = descents_proj.geometry.union_all()
    descent_buffer = descent_union.buffer(buffer_m)

    rep.rappel_points = _score_points(points_gpkg, descent_buffer, proj_crs)

    polys_gpkg = out_dir / "drainage_polys.gpkg"
    if polys_gpkg.exists():
        rep.drainage_polys = _score_polys(polys_gpkg, descent_union, proj_crs)

    routes_gpkg = out_dir / "candidate_routes.gpkg"
    if routes_gpkg.exists():
        rep.candidate_routes = _score_polys(routes_gpkg, descent_union, proj_crs)

    # Named-point proximity (uses all detected rappel points, not just in-buffer ones).
    pts = gpd.read_file(points_gpkg).to_crs(proj_crs)
    rep.nearest_to_named_points = _nearest_named_distances(features.points, pts, proj_crs)

    return rep


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _format_report(r: ValidationReport) -> str:
    lines = [
        f"KML:       {r.kml_path}",
        f"out_dir:   {r.out_dir}",
        f"buffer:    {r.buffer_m:.1f} m",
        f"descent:   {r.descent_names}  length={r.descent_length_m:.1f} m",
    ]
    if r.rappel_points:
        rp = r.rappel_points
        inside_h = rp["inside"]["heights"]
        outside_h = rp["outside"]["heights"]
        lines.append(f"rappel_points: total={rp['total']}")
        lines.append(
            f"  inside  buffer: count={rp['inside']['count']:>6}  "
            f"heights median={inside_h.get('median', 0):.1f} "
            f"mean={inside_h.get('mean', 0):.1f} "
            f"p90={inside_h.get('p90', 0):.1f} "
            f"max={inside_h.get('max', 0):.1f}"
        )
        lines.append(
            f"  outside buffer: count={rp['outside']['count']:>6}  "
            f"heights median={outside_h.get('median', 0):.1f} "
            f"mean={outside_h.get('mean', 0):.1f} "
            f"p90={outside_h.get('p90', 0):.1f} "
            f"max={outside_h.get('max', 0):.1f}"
        )
    if r.drainage_polys:
        dp = r.drainage_polys
        lines.append(
            f"drainage_polys: total={dp['total']}  "
            f"intersecting_descent={dp['intersecting_descent']}  "
            f"area median={dp['area_m2']['median']:.0f} m²  max={dp['area_m2']['max']:.0f} m²"
        )
    if r.candidate_routes:
        cr = r.candidate_routes
        lines.append(
            f"candidate_routes: total={cr['total']}  "
            f"intersecting_descent={cr['intersecting_descent']}"
        )
    if r.nearest_to_named_points:
        lines.append("nearest_detection_to_named_points:")
        for row in r.nearest_to_named_points:
            lines.append(
                f"  {row['name']:<20} -> {row['distance_to_nearest_detection_m']:>8.1f} m"
            )
    for w in r.warnings:
        lines.append(f"WARN: {w}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="canyongis-validate",
        description="Score canyongis output against a KML of known canyon/approach lines.",
    )
    p.add_argument("--kml", type=Path, required=True, help="Ground-truth KML (e.g. CalTopo export).")
    p.add_argument("--out-dir", type=Path, required=True, help="Directory containing canyongis output (*.gpkg).")
    p.add_argument("--buffer", type=float, default=25.0, help="Buffer radius in meters around descent lines (default: 25).")
    p.add_argument("--descent-name", type=str, default=None, help="Restrict scoring to a single descent line by name.")
    p.add_argument("--json", type=Path, default=None, help="Also write the report as JSON to this path.")
    return p


def cli(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    report = validate_run(
        kml_path=args.kml,
        out_dir=args.out_dir,
        buffer_m=args.buffer,
        descent_name=args.descent_name,
    )
    print(_format_report(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2))


if __name__ == "__main__":
    cli()

"""Validate canyongis detection output against a ground-truth KML (e.g. CalTopo export).

Three complementary scoring modes:

1. **Line buffer** — buffer the descent LineString and count detected rappels inside.
   Good when the KML line is reasonably accurate, but hand-drawn traces often sit
   20-80 m off the real flow-accumulation thalweg, making tight buffers misleading.

2. **Stream buffer** (``--stream-buffer-m N``) — buffer the *computed* stream
   network (flow_accum.tif, thresholded) by N meters. Sidesteps hand-drawn-line
   error entirely; answers "how many detections land on the actual channel?"

3. **Ground-truth rappel points** — KML ``Point`` placemarks whose name contains
   ``rappel`` (case-insensitive) are treated as individual known rappels. For
   each, report the distance to the nearest detected rappel and whether that
   distance is within a configurable match radius.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, MultiLineString, Point  # Point used in tests/type hints

KML_NS = "{http://www.opengis.net/kml/2.2}"

# Name substrings (case-insensitive) used to classify KML lines.
APPROACH_TOKENS = ("approach", "shortcut", "bushwack", "bushwhack", "exit", "trail")
DESCENT_TOKENS = ("descent", "rappel", "logjam", "canyon", "gorge", "creek")
# Tokens that disqualify a line from being a descent or approach even if it contains
# matching tokens (e.g. "Zion Canyon Shuttle" — the shuttle road is neither).
NON_ROUTE_TOKENS = ("shuttle", "road", "highway", "freeway", "parkway")

# Names that identify a KML Point as a ground-truth rappel location.
RAPPEL_POINT_PATTERN = re.compile(
    r"\brappel\b|\brap\b|\bdrop[\s-]*in\b|pour[-\s]?off", re.IGNORECASE
)


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

    def rappel_points(self) -> gpd.GeoDataFrame:
        """Subset of points whose name identifies them as a known rappel location."""
        if self.points.empty:
            return self.points
        mask = self.points["name"].apply(lambda n: bool(RAPPEL_POINT_PATTERN.search(n or "")))
        return self.points[mask].reset_index(drop=True)


def _classify(name: str) -> str:
    n = name.lower()
    if any(tok in n for tok in NON_ROUTE_TOKENS):
        return "unknown"
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
    stream_buffer_m: float | None = None
    descent_names: list[str] = field(default_factory=list)
    descent_length_m: float = 0.0
    rappel_points: dict[str, Any] | None = None
    rappel_points_stream: dict[str, Any] | None = None
    drainage_polys: dict[str, Any] | None = None
    candidate_routes: dict[str, Any] | None = None
    nearest_to_named_points: list[dict[str, Any]] = field(default_factory=list)
    ground_truth_rappels: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kml_path": self.kml_path,
            "out_dir": self.out_dir,
            "buffer_m": self.buffer_m,
            "stream_buffer_m": self.stream_buffer_m,
            "descent": {
                "names": self.descent_names,
                "length_m": self.descent_length_m,
            },
            "rappel_points_line": self.rappel_points,
            "rappel_points_stream": self.rappel_points_stream,
            "drainage_polys": self.drainage_polys,
            "candidate_routes": self.candidate_routes,
            "nearest_to_named_points": self.nearest_to_named_points,
            "ground_truth_rappels": self.ground_truth_rappels,
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


def _score_points_against_geom(pts_proj: gpd.GeoDataFrame, buffer_geom: Any) -> dict[str, Any]:
    """Score detected points against a pre-built (projected) buffer polygon."""
    inside_mask = pts_proj.geometry.intersects(buffer_geom)
    inside = pts_proj[inside_mask]
    outside = pts_proj[~inside_mask]

    height_col = "drop_height_m" if "drop_height_m" in pts_proj.columns else None

    def _h(df):
        if height_col is None or df.empty:
            return np.array([], dtype=np.float32)
        return df[height_col].to_numpy(dtype=np.float32)

    return {
        "total": len(pts_proj),
        "inside": {
            "count": len(inside),
            "heights": _describe_heights(_h(inside)),
        },
        "outside": {
            "count": len(outside),
            "heights": _describe_heights(_h(outside)),
        },
    }


def _score_polys(polys_gpkg: Path, descent_line_proj: Any, proj_crs: Any) -> dict[str, Any]:
    polys = gpd.read_file(polys_gpkg)
    if polys.empty:
        return {"total": 0, "intersecting_descent": 0}
    polys_proj = polys.to_crs(proj_crs) if polys.crs != proj_crs else polys
    hits = polys_proj[polys_proj.geometry.intersects(descent_line_proj)]
    areas = polys_proj.geometry.area.to_numpy()
    return {
        "total": len(polys_proj),
        "intersecting_descent": len(hits),
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
    names_of_interest: tuple[str, ...],
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


def _score_ground_truth_rappels(
    gt_pts_wgs84: gpd.GeoDataFrame,
    detected_pts_proj: gpd.GeoDataFrame,
    proj_crs: Any,
    match_radius_m: float,
) -> dict[str, Any]:
    """For each ground-truth rappel point, find the nearest detection and report
    whether it is within `match_radius_m` (a "hit")."""
    if gt_pts_wgs84.empty:
        return {"count": 0, "matches": [], "match_rate": None}
    gt_proj = gt_pts_wgs84.to_crs(proj_crs)
    if detected_pts_proj.empty:
        rows = [
            {"name": r["name"], "nearest_m": float("inf"), "hit": False, "nearest_drop_m": None}
            for _, r in gt_proj.iterrows()
        ]
        return {
            "count": len(rows),
            "matches": rows,
            "match_rate": 0.0,
            "match_radius_m": match_radius_m,
        }

    # Build a spatial structure for nearest lookup
    det_geoms = detected_pts_proj.geometry.to_numpy()
    det_heights = (
        detected_pts_proj["drop_height_m"].to_numpy()
        if "drop_height_m" in detected_pts_proj.columns
        else None
    )

    matches: list[dict[str, Any]] = []
    hit_count = 0
    for _, gt in gt_proj.iterrows():
        d_arr = np.array([gt.geometry.distance(g) for g in det_geoms], dtype=np.float64)
        idx = int(np.argmin(d_arr))
        d_min = float(d_arr[idx])
        hit = d_min <= match_radius_m
        if hit:
            hit_count += 1
        matches.append(
            {
                "name": gt["name"],
                "nearest_m": d_min,
                "hit": hit,
                "nearest_drop_m": float(det_heights[idx]) if det_heights is not None else None,
            }
        )
    return {
        "count": len(matches),
        "matches": matches,
        "match_rate": hit_count / len(matches),
        "match_radius_m": match_radius_m,
    }


def _stream_buffer_mask(
    out_dir: Path, flow_accum_threshold: int, buffer_m: float
) -> tuple[np.ndarray, Any, Any] | tuple[None, None, None]:
    """Build a boolean raster mask dilated around the computed stream network.

    Uses morphological dilation on the stream raster — O(raster_pixels) with no
    Shapely allocations. Returns (dilated_mask, transform, crs) or (None, None, None).
    """
    fa_path = out_dir / "flow_accum.tif"
    if not fa_path.exists():
        return None, None, None
    import rasterio
    from scipy import ndimage as ndi

    with rasterio.open(fa_path) as src:
        fa = src.read(1)
        tr = src.transform
        crs = src.crs
        pixel_size = abs(float(tr.a))

    stream_mask = fa >= flow_accum_threshold
    if not stream_mask.any():
        return None, tr, crs

    buf_px = max(1, round(buffer_m / pixel_size))
    r = buf_px
    yy, xx = np.ogrid[-r : r + 1, -r : r + 1]
    struct = (xx * xx + yy * yy) <= r * r
    dilated = ndi.binary_dilation(stream_mask, structure=struct)
    return dilated, tr, crs


def _score_points_against_mask(
    pts_proj: gpd.GeoDataFrame,
    mask: np.ndarray,
    mask_transform: Any,
    mask_crs: Any,
) -> dict[str, Any]:
    """Score detected points against a boolean raster mask via row/col lookup."""
    from rasterio.transform import rowcol

    pts_in_crs = pts_proj if pts_proj.crs == mask_crs else pts_proj.to_crs(mask_crs)
    nrows, ncols = mask.shape
    inside_flags = []
    for geom in pts_in_crs.geometry:
        row, col = rowcol(mask_transform, geom.x, geom.y)
        inside_flags.append(bool(0 <= row < nrows and 0 <= col < ncols and mask[row, col]))

    inside_arr = np.array(inside_flags)
    inside = pts_proj[inside_arr]
    outside = pts_proj[~inside_arr]

    height_col = "drop_height_m" if "drop_height_m" in pts_proj.columns else None

    def _h(df):
        if height_col is None or df.empty:
            return np.array([], dtype=np.float32)
        return df[height_col].to_numpy(dtype=np.float32)

    return {
        "total": len(pts_proj),
        "inside": {"count": len(inside), "heights": _describe_heights(_h(inside))},
        "outside": {"count": len(outside), "heights": _describe_heights(_h(outside))},
    }


def validate_run(
    kml_path: str | Path,
    out_dir: str | Path,
    buffer_m: float = 25.0,
    descent_name: str | None = None,
    stream_buffer_m: float | None = None,
    flow_accum_threshold: int = 500,
    rappel_match_m: float = 25.0,
) -> ValidationReport:
    """Compute validation metrics for a canyongis run in `out_dir` against `kml_path`."""
    out_dir = Path(out_dir)
    kml_path = Path(kml_path)

    features = parse_kml(kml_path)
    rep = ValidationReport(
        kml_path=str(kml_path),
        out_dir=str(out_dir),
        buffer_m=buffer_m,
        stream_buffer_m=stream_buffer_m,
    )

    # Pick descent reference
    descents = features.descent_lines
    if descent_name is not None:
        descents = descents[descents["name"] == descent_name]
        if descents.empty:
            rep.warnings.append(
                f"descent_name={descent_name!r} not found; known descents: "
                f"{list(features.descent_lines['name'])}"
            )

    points_gpkg = out_dir / "rappel_points.gpkg"
    if not points_gpkg.exists():
        rep.warnings.append(f"{points_gpkg} not found — cannot score rappel points")
        return rep

    ref = gpd.read_file(points_gpkg, rows=1)
    if ref.crs is None or not ref.crs.is_projected:
        rep.warnings.append(
            f"rappel_points CRS is not projected ({ref.crs}); metrics may be incorrect"
        )
    proj_crs = ref.crs
    pts_proj = gpd.read_file(points_gpkg).to_crs(proj_crs)

    # --- Line-buffer scoring (original mode) ---
    if not descents.empty:
        descents_proj = descents.to_crs(proj_crs)
        rep.descent_names = list(descents_proj["name"])
        rep.descent_length_m = float(descents_proj.geometry.length.sum())
        descent_union = descents_proj.geometry.union_all()
        descent_buffer = descent_union.buffer(buffer_m)
        rep.rappel_points = _score_points_against_geom(pts_proj, descent_buffer)

        polys_gpkg = out_dir / "drainage_polys.gpkg"
        if polys_gpkg.exists():
            rep.drainage_polys = _score_polys(polys_gpkg, descent_union, proj_crs)

        routes_gpkg = out_dir / "candidate_routes.gpkg"
        if routes_gpkg.exists():
            rep.candidate_routes = _score_polys(routes_gpkg, descent_union, proj_crs)

    # --- Stream-buffer scoring (raster dilation — no Shapely Point allocations) ---
    if stream_buffer_m is not None:
        smask, str_tr, str_crs = _stream_buffer_mask(
            out_dir, flow_accum_threshold=flow_accum_threshold, buffer_m=stream_buffer_m
        )
        if smask is None:
            rep.warnings.append(
                "stream-buffer mode requested but flow_accum.tif not found or empty"
            )
        else:
            rep.rappel_points_stream = _score_points_against_mask(pts_proj, smask, str_tr, str_crs)

    # --- Named-point distances (legacy, kept for context) ---
    legacy_names = (
        "Canyon Drop-in",
        "Top of Gorge",
        "Bottom of Gorge",
        "Bramble Drop In",
        "Last Rappel",
    )
    rep.nearest_to_named_points = _nearest_named_distances(
        features.points, pts_proj, proj_crs, legacy_names
    )

    # --- Ground-truth rappel points (rappel/drop-in waypoints) ---
    gt = features.rappel_points()
    if not gt.empty:
        rep.ground_truth_rappels = _score_ground_truth_rappels(
            gt, pts_proj, proj_crs, match_radius_m=rappel_match_m
        )

    return rep


# -----------------------------------------------------------------------------
# KML export
# -----------------------------------------------------------------------------

_KML_COLORS = {
    "descent": "ff0000ff",  # red (aabbggrr)
    "approach": "ffff7800",  # blue-ish
    "detected": "ff00ff00",  # green
    "route": "ff00aaff",  # amber
    "gt_rappel": "ff00ffff",  # yellow
}


def _kml_line_placemark(name: str, geom_wgs84: Any, color: str) -> str:
    coords = " ".join(f"{x},{y},0" for x, y in geom_wgs84.coords)
    return (
        f"<Placemark><name>{name}</name>"
        f"<Style><LineStyle><color>{color}</color><width>3</width></LineStyle></Style>"
        f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString>"
        f"</Placemark>\n"
    )


def _kml_point_placemark(name: str, lon: float, lat: float, color: str, desc: str = "") -> str:
    return (
        f"<Placemark><name>{name}</name>"
        f"<description>{desc}</description>"
        f"<Style><IconStyle><color>{color}</color>"
        f"<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon>"
        f"</IconStyle></Style>"
        f"<Point><coordinates>{lon},{lat},0</coordinates></Point>"
        f"</Placemark>\n"
    )


def export_validation_kml(
    features: KmlFeatures,
    detected_rappels: gpd.GeoDataFrame,
    out_path: Path,
    candidate_routes_gpkg: Path | None = None,
) -> None:
    """Write a combined KML: Ground Truth lines/points, Detected Rappels, Candidate Routes."""
    import xml.etree.ElementTree as _ET  # noqa: F401 (already imported at module level)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        "<name>CanyonGIS Validation</name>",
    ]

    # --- Ground Truth folder ---
    lines.append("<Folder><name>Ground Truth</name>")
    for _, row in features.descent_lines.iterrows():
        geom = row.geometry
        coords = (
            list(geom.coords)
            if geom.geom_type == "LineString"
            else [c for part in geom.geoms for c in part.coords]
        )
        ls = type("_", (), {"coords": coords})()
        lines.append(_kml_line_placemark(row["name"], ls, _KML_COLORS["descent"]))

    for _, row in features.approach_lines.iterrows():
        geom = row.geometry
        coords = (
            list(geom.coords)
            if geom.geom_type == "LineString"
            else [c for part in geom.geoms for c in part.coords]
        )
        ls = type("_", (), {"coords": coords})()
        lines.append(_kml_line_placemark(row["name"], ls, _KML_COLORS["approach"]))

    gt_pts = features.rappel_points()
    if not gt_pts.empty:
        for _, row in gt_pts.iterrows():
            lines.append(
                _kml_point_placemark(
                    row["name"], row.geometry.x, row.geometry.y, _KML_COLORS["gt_rappel"]
                )
            )
    lines.append("</Folder>")

    # --- Detected Rappels folder ---
    lines.append("<Folder><name>Detected Rappels</name>")
    if not detected_rappels.empty:
        det_wgs = detected_rappels.to_crs("EPSG:4326")
        for i, row in det_wgs.iterrows():
            drop = row.get("drop_height_m", None)
            label = f"R{i + 1} ({drop:.0f}m)" if drop is not None else f"R{i + 1}"
            desc = f"drop_height_m: {drop:.1f}" if drop is not None else ""
            lines.append(
                _kml_point_placemark(
                    label, row.geometry.x, row.geometry.y, _KML_COLORS["detected"], desc
                )
            )
    lines.append("</Folder>")

    # --- Candidate Routes folder (full polygons) ---
    if candidate_routes_gpkg is not None and candidate_routes_gpkg.exists():
        routes = gpd.read_file(candidate_routes_gpkg).to_crs("EPSG:4326")
        if not routes.empty:
            lines.append("<Folder><name>Candidate Routes</name>")
            for i, row in routes.iterrows():
                desc_m = row.get("total_descent_m", None)
                label = f"Route {i + 1}"
                desc = f"descent: {desc_m:.0f}m" if desc_m is not None else ""
                geom = row.geometry
                polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
                for poly in polys:
                    outer = " ".join(f"{x},{y},0" for x, y in poly.exterior.coords)
                    inner_rings = "".join(
                        f"<innerBoundaryIs><LinearRing><coordinates>"
                        f"{' '.join(f'{x},{y},0' for x, y in ring.coords)}"
                        f"</coordinates></LinearRing></innerBoundaryIs>"
                        for ring in poly.interiors
                    )
                    lines.append(
                        f"<Placemark><name>{label}</name><description>{desc}</description>"
                        f"<Style><LineStyle><color>{_KML_COLORS['route']}</color><width>2</width></LineStyle>"
                        f"<PolyStyle><color>4000aaff</color></PolyStyle></Style>"
                        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{outer}</coordinates>"
                        f"</LinearRing></outerBoundaryIs>{inner_rings}</Polygon></Placemark>\n"
                    )
            lines.append("</Folder>")

    lines.append("</Document></kml>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _format_report(r: ValidationReport) -> str:
    lines = [
        f"KML:       {r.kml_path}",
        f"out_dir:   {r.out_dir}",
        f"line buffer: {r.buffer_m:.1f} m"
        + (f"   stream buffer: {r.stream_buffer_m:.1f} m" if r.stream_buffer_m is not None else ""),
    ]
    if r.descent_names:
        lines.append(f"descent:   {r.descent_names}  length={r.descent_length_m:.1f} m")

    def _block(label: str, rp: dict[str, Any]):
        inside_h = rp["inside"]["heights"]
        outside_h = rp["outside"]["heights"]
        lines.append(f"{label}: total={rp['total']}")
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

    if r.rappel_points:
        _block("rappel_points (vs descent line)", r.rappel_points)
    if r.rappel_points_stream:
        _block("rappel_points (vs stream network)", r.rappel_points_stream)
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
    if r.ground_truth_rappels:
        gt = r.ground_truth_rappels
        lines.append(
            f"ground_truth_rappels: count={gt['count']}  "
            f"match_rate={gt['match_rate'] * 100:.0f}% @ {gt['match_radius_m']:.0f}m"
        )
        for m in gt["matches"]:
            flag = "HIT" if m["hit"] else "miss"
            drop = (
                f"drop={m['nearest_drop_m']:.1f}m" if m["nearest_drop_m"] is not None else "drop=?"
            )
            lines.append(f"  {m['name']:<26} -> {m['nearest_m']:>7.1f} m  [{flag}]  {drop}")
    if r.nearest_to_named_points:
        lines.append("nearest_detection_to_named_points:")
        for row in r.nearest_to_named_points:
            lines.append(f"  {row['name']:<20} -> {row['distance_to_nearest_detection_m']:>8.1f} m")
    for w in r.warnings:
        lines.append(f"WARN: {w}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="canyongis-validate",
        description="Score canyongis output against a KML of known canyon/approach lines.",
    )
    p.add_argument(
        "--kml", type=Path, required=True, help="Ground-truth KML (e.g. CalTopo export)."
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory containing canyongis output (*.gpkg, *.tif).",
    )
    p.add_argument(
        "--buffer",
        type=float,
        default=25.0,
        help="Buffer radius (m) around descent lines (default: 25).",
    )
    p.add_argument(
        "--descent-name",
        type=str,
        default=None,
        help="Restrict line-buffer scoring to a single descent line by name.",
    )
    p.add_argument(
        "--stream-buffer-m",
        type=float,
        default=None,
        help="If set, also score rappels against a buffer around the computed stream network (reads flow_accum.tif).",
    )
    p.add_argument(
        "--flow-accum-threshold",
        type=int,
        default=500,
        help="Flow accumulation threshold for stream extraction (default: 500).",
    )
    p.add_argument(
        "--rappel-match-m",
        type=float,
        default=25.0,
        help="Match radius for ground-truth rappel points (default: 25).",
    )
    p.add_argument(
        "--json", type=Path, default=None, help="Also write the report as JSON to this path."
    )
    p.add_argument(
        "--output-kml",
        type=Path,
        default=None,
        help="Write a combined KML (GT lines/points + detected rappels + candidate routes) to this path.",
    )
    return p


def cli(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    report = validate_run(
        kml_path=args.kml,
        out_dir=args.out_dir,
        buffer_m=args.buffer,
        descent_name=args.descent_name,
        stream_buffer_m=args.stream_buffer_m,
        flow_accum_threshold=args.flow_accum_threshold,
        rappel_match_m=args.rappel_match_m,
    )
    print(_format_report(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2))
    if args.output_kml:
        features = parse_kml(args.kml)
        out_dir = Path(args.out_dir)
        det = gpd.GeoDataFrame()
        rp_path = out_dir / "rappel_points.gpkg"
        if rp_path.exists():
            det = gpd.read_file(rp_path)
        routes_path = out_dir / "candidate_routes.gpkg"
        export_validation_kml(
            features,
            det,
            args.output_kml,
            candidate_routes_gpkg=routes_path if routes_path.exists() else None,
        )
        print(f"KML written to {args.output_kml}")


if __name__ == "__main__":
    cli()

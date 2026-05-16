"""Tests for canyongis.validate — KML parser/classifier + metric calculations."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point

from canyongis.validate import (
    RAPPEL_POINT_PATTERN,
    _classify,
    _describe_heights,
    parse_kml,
    validate_run,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kml"
FIXTURE_KML = FIXTURE_DIR / "Great_Falls_of_the_Fox.kml"
FIXTURE_KML_FOX = FIXTURE_DIR / "Fantastic_Mr._Fox_Canyon.kml"


def test_classifier_heuristics():
    assert _classify("Full Descent") == "descent"
    assert _classify("Fox Creek Logjams") == "descent"
    assert _classify("Approach") == "approach"
    assert _classify("Bramble Canyon Approach") == "approach"  # approach wins over canyon
    assert _classify("Full Approach") == "approach"
    assert _classify("Ridge Approach") == "approach"
    assert _classify("Brennan Shortcut") == "approach"
    assert _classify("Standard Exit") == "approach"
    assert _classify("Express Exit") == "approach"
    assert _classify("Top of Gorge") == "descent"
    assert _classify("Random Mystery Line") == "unknown"
    assert _classify("Zion Canyon Shuttle") == "unknown"
    assert _classify("Highway 9 to Trailhead") == "unknown"


def test_describe_heights_empty():
    assert _describe_heights(np.array([])) == {"count": 0}


def test_describe_heights_stats():
    h = np.array([10.0, 20.0, 30.0, 40.0, 50.0], dtype=np.float32)
    d = _describe_heights(h)
    assert d["count"] == 5
    assert d["min"] == 10.0
    assert d["max"] == 50.0
    assert d["median"] == 30.0
    assert d["mean"] == 30.0


@pytest.mark.skipif(not FIXTURE_KML.exists(), reason="Great_Falls_of_the_Fox.kml not present")
def test_parse_real_kml_has_descent_and_approach():
    feats = parse_kml(FIXTURE_KML)
    desc_names = set(feats.descent_lines["name"])
    appr_names = set(feats.approach_lines["name"])

    assert "Full Descent" in desc_names
    assert "Fox Creek Logjams" in desc_names
    assert "Approach" in appr_names
    assert "Bramble Canyon Approach" in appr_names
    assert "Full Approach" in appr_names

    # Full Descent should be a non-trivial line (~1 km)
    full_descent = feats.descent_lines[feats.descent_lines["name"] == "Full Descent"]
    assert len(full_descent) == 1
    wgs_len_deg = full_descent.geometry.iloc[0].length
    # ~0.009 deg latitude at 34°N corresponds to ~1 km
    assert 0.005 < wgs_len_deg < 0.02

    # A selection of named points should be present
    pt_names = set(feats.points["name"])
    assert "Last Rappel" in pt_names
    assert "Canyon Drop-in" in pt_names


def test_validate_run_missing_output_dir(tmp_path):
    # No rappel_points.gpkg in tmp_path; should warn, not crash.
    fake_kml = tmp_path / "fake.kml"
    fake_kml.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document><Placemark><name>Full Descent</name><LineString>"
        "<coordinates>-118.18,34.31,0 -118.179,34.305,0</coordinates>"
        "</LineString></Placemark></Document></kml>"
    )
    rep = validate_run(fake_kml, tmp_path, buffer_m=25.0)
    assert rep.rappel_points is None
    assert any("rappel_points.gpkg" in w for w in rep.warnings)


def test_score_points_inside_outside(tmp_path):
    """Synthetic scenario: 3 points inside the 25m buffer, 2 far away."""
    # Build a short descent line in UTM 11N (matches the project's DEM CRS)
    crs = "EPSG:26911"
    line = LineString([(400000, 3790000), (400100, 3790000)])
    descent_gdf = gpd.GeoDataFrame({"name": ["Fake Descent"], "geometry": [line]}, crs=crs)

    inside_pts = [Point(400010, 3790005), Point(400050, 3790010), Point(400090, 3789995)]
    outside_pts = [Point(400500, 3790500), Point(399000, 3789000)]
    heights = [30.0, 45.0, 10.0, 200.0, 250.0]
    points = gpd.GeoDataFrame(
        {
            "drop_height_m": heights,
            "geometry": inside_pts + outside_pts,
        },
        crs=crs,
    )

    out = tmp_path / "out"
    out.mkdir()
    points.to_file(out / "rappel_points.gpkg", driver="GPKG")

    kml = tmp_path / "synth.kml"
    descent_wgs = descent_gdf.to_crs("EPSG:4326")
    coords = " ".join(f"{x},{y},0" for x, y in descent_wgs.geometry.iloc[0].coords)
    kml.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
        f"<Document><Placemark><name>Full Descent</name><LineString>"
        f"<coordinates>{coords}</coordinates>"
        "</LineString></Placemark></Document></kml>"
    )

    rep = validate_run(kml, out, buffer_m=25.0)
    assert rep.rappel_points is not None
    assert rep.rappel_points["total"] == 5
    assert rep.rappel_points["inside"]["count"] == 3
    assert rep.rappel_points["outside"]["count"] == 2
    assert rep.rappel_points["inside"]["heights"]["median"] == 30.0
    assert rep.rappel_points["outside"]["heights"]["min"] == 200.0


def test_rappel_point_pattern_recognition():
    assert RAPPEL_POINT_PATTERN.search("160ft rappel")
    assert RAPPEL_POINT_PATTERN.search("Final rappel")
    assert RAPPEL_POINT_PATTERN.search("Rappel sequence start")
    assert RAPPEL_POINT_PATTERN.search("Drop in")
    assert RAPPEL_POINT_PATTERN.search("Canyon Drop-in")
    assert RAPPEL_POINT_PATTERN.search("Pour-off 1")
    # Not rappel points:
    assert not RAPPEL_POINT_PATTERN.search("Approach")
    assert not RAPPEL_POINT_PATTERN.search("Parking")
    assert not RAPPEL_POINT_PATTERN.search("Summit")


@pytest.mark.skipif(not FIXTURE_KML_FOX.exists(), reason="Fantastic_Mr._Fox_Canyon.kml not present")
def test_parse_fantastic_mr_fox_kml():
    feats = parse_kml(FIXTURE_KML_FOX)
    # Three classified lines: Descent (+Exit is approach), Approach, Exit.
    desc_names = set(feats.descent_lines["name"])
    appr_names = set(feats.approach_lines["name"])
    assert "Descent" in desc_names
    assert "Approach" in appr_names
    assert "Exit" in appr_names

    gt = feats.rappel_points()
    gt_names = set(gt["name"])
    # Rappel-named waypoints should all be picked up.
    assert "160ft rappel" in gt_names
    assert "Rappel sequence start" in gt_names
    assert "End rappel sequence" in gt_names
    assert "Final rappel" in gt_names
    assert "Drop in" in gt_names
    # Non-rappel points should NOT be in the ground-truth set.
    assert "Park" not in gt_names
    assert "Summit" not in gt_names
    assert "Shuttle parking" not in gt_names


def test_ground_truth_rappels_scoring(tmp_path):
    """Detections near each ground-truth rappel count as hits."""
    crs = "EPSG:26911"
    # Ground-truth rappels at two locations
    gt_coords_utm = [(400000, 3790000), (400100, 3790050)]
    # Build a KML in WGS84
    from pyproj import Transformer

    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    kml_coords = [t.transform(x, y) for x, y in gt_coords_utm]

    kml = tmp_path / "synth.kml"
    kml.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Rappel 1</name><Point><coordinates>{kml_coords[0][0]},{kml_coords[0][1]},0</coordinates></Point></Placemark>"
        f"<Placemark><name>Rappel 2</name><Point><coordinates>{kml_coords[1][0]},{kml_coords[1][1]},0</coordinates></Point></Placemark>"
        "</Document></kml>"
    )

    # Detections: one 10m from Rappel 1 (HIT), one 100m from Rappel 2 (miss)
    det = gpd.GeoDataFrame(
        {
            "drop_height_m": [15.0, 30.0],
            "geometry": [Point(400010, 3790000), Point(400200, 3790050)],
        },
        crs=crs,
    )
    out = tmp_path / "out"
    out.mkdir()
    det.to_file(out / "rappel_points.gpkg", driver="GPKG")

    rep = validate_run(kml, out, rappel_match_m=25.0)
    assert rep.ground_truth_rappels is not None
    gt = rep.ground_truth_rappels
    assert gt["count"] == 2
    # Rappel 1 should be a hit (10m away, within 25m); Rappel 2 should miss (100m).
    matches = {m["name"]: m for m in gt["matches"]}
    assert matches["Rappel 1"]["hit"] is True
    assert matches["Rappel 1"]["nearest_m"] == pytest.approx(10.0, abs=0.1)
    assert matches["Rappel 2"]["hit"] is False
    assert matches["Rappel 2"]["nearest_m"] == pytest.approx(100.0, abs=0.1)
    assert gt["match_rate"] == 0.5

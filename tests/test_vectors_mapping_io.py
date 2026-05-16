from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon

from canyongis.io import _write_kml_lines, _write_kml_points
from canyongis.mapping import render_folium_map
from canyongis.preview_tiles import _downsample_by_factor, _estimate_zoom, _rgba_array
from canyongis.vectors import score_routes


def test_score_routes_empty_returns_metric_columns() -> None:
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:26911")

    scored = score_routes(empty, empty, np.zeros((2, 2), dtype=np.float32), from_origin(0, 2, 1, 1))

    assert scored.empty
    assert {"total_descent_m", "grade", "cliff_count", "length_proxy_m"}.issubset(scored.columns)


def test_score_routes_computes_descent_grade_and_cliff_count() -> None:
    drainage = gpd.GeoDataFrame(
        geometry=[Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])], crs="EPSG:26911"
    )
    rappels = gpd.GeoDataFrame(geometry=[Point(1, 1), Point(100, 100)], crs="EPSG:26911")
    elev = np.array([[10, 12], [20, np.nan]], dtype=np.float32)

    scored = score_routes(drainage, rappels, elev, from_origin(0, 2, 1, 1))

    assert scored.loc[0, "total_descent_m"] == pytest.approx(10.0)
    assert scored.loc[0, "length_proxy_m"] == pytest.approx(2.0)
    assert scored.loc[0, "grade"] == pytest.approx(5.0)
    assert scored.loc[0, "cliff_count"] == 1


def test_score_routes_handles_all_nodata_polygon() -> None:
    drainage = gpd.GeoDataFrame(
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])], crs="EPSG:26911"
    )
    elev = np.full((1, 1), np.nan, dtype=np.float32)

    scored = score_routes(drainage, gpd.GeoDataFrame(geometry=[], crs="EPSG:26911"), elev, from_origin(0, 1, 1, 1))

    assert scored.loc[0, "total_descent_m"] == 0
    assert scored.loc[0, "grade"] == 0
    assert scored.loc[0, "cliff_count"] == 0
    assert scored.loc[0, "length_proxy_m"] == 0


def test_write_kml_lines_and_points_escape_labels(tmp_path: Path) -> None:
    line_path = tmp_path / "lines.kml"
    point_path = tmp_path / "points.kml"
    lines = gpd.GeoDataFrame(
        {"name": ["A&B"], "geometry": [LineString([(0, 0), (1, 1)])]}, crs="EPSG:4326"
    )
    points = gpd.GeoDataFrame(
        {"prob": [0.75], "geometry": [Point(1, 2)]}, crs="EPSG:4326"
    )

    _write_kml_lines(lines, line_path)
    _write_kml_points(points, point_path)

    assert "A&amp;B" in line_path.read_text()
    assert "0.0,0.0,0 1.0,1.0,0" in line_path.read_text()
    assert "Point 1 (p=0.75)" in point_path.read_text()
    assert "1.0,2.0,0" in point_path.read_text()


def test_render_folium_map_writes_centerlines_and_limited_points(tmp_path: Path) -> None:
    out = tmp_path / "map.html"
    lines = gpd.GeoDataFrame(
        {"mean_prob": [0.9], "length_m": [123.0], "geometry": [LineString([(0, 0), (1, 1)])]},
        crs="EPSG:4326",
    )
    points = gpd.GeoDataFrame(
        {"prob": [0.1, 0.9], "geometry": [Point(0, 0), Point(1, 1)]}, crs="EPSG:4326"
    )

    render_folium_map(points, lines, (0, 0, 2, 2), str(out), max_points=1)

    html = out.read_text()
    assert "ML centerlines" in html
    assert "Rappel candidates" in html
    assert "p = 0.90" in html
    assert "p = 0.10" not in html


def test_preview_tile_helpers() -> None:
    from PIL import Image

    image = Image.new("RGBA", (4, 2), (10, 20, 30, 255))
    mask = np.array([[True, False, True, True], [False, False, True, True]])

    rgba = _rgba_array(image, mask)

    assert rgba.shape == (2, 4, 4)
    assert rgba[0, 1, 3] == 0
    assert rgba[0, 0, 3] == 255
    assert _downsample_by_factor(image, 2).size == (2, 1)
    assert _downsample_by_factor(image, 1).size == image.size
    assert _estimate_zoom(256, 256, (-180, -85, 180, 85)) >= 0


def test_rgba_array_rejects_bad_mask_shape() -> None:
    from PIL import Image

    with pytest.raises(ValueError, match="valid_mask shape"):
        _rgba_array(Image.new("RGBA", (2, 2)), np.ones((1, 2), dtype=bool))

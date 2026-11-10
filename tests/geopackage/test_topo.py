"""Offline topo layers: OSM parsing, contours, and tile rendering on small synthetic inputs."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pyproj import Transformer

from canyongis.geopackage import topo
from canyongis.geopackage.tiles import lonlat_to_tile

LON, LAT = -117.55, 34.25
UTM = Transformer.from_crs(4326, 26911, always_xy=True)

OSM_XML = f"""<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="{LAT}" lon="{LON - 0.004}"/>
  <node id="2" lat="{LAT}" lon="{LON + 0.004}"/>
  <node id="3" lat="{LAT + 0.002}" lon="{LON - 0.004}"/>
  <node id="4" lat="{LAT + 0.002}" lon="{LON + 0.004}"/>
  <node id="5" lat="{LAT - 0.002}" lon="{LON - 0.001}"/>
  <node id="6" lat="{LAT - 0.002}" lon="{LON + 0.001}"/>
  <node id="7" lat="{LAT - 0.001}" lon="{LON + 0.001}"/>
  <node id="8" lat="{LAT - 0.001}" lon="{LON - 0.001}"/>
  <node id="9" lat="{LAT + 0.001}" lon="{LON}">
    <tag k="natural" v="peak"/><tag k="name" v="Test Peak"/><tag k="ele" v="1234"/>
  </node>
  <way id="10"><nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="track"/><tag k="name" v="Old Mine Road"/></way>
  <way id="11"><nd ref="3"/><nd ref="4"/><tag k="highway" v="path"/></way>
  <way id="12"><nd ref="1"/><nd ref="3"/><tag k="waterway" v="stream"/></way>
  <way id="13"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/>
    <tag k="natural" v="water"/></way>
  <way id="14"><nd ref="1"/><nd ref="2"/><nd ref="4"/><nd ref="3"/><nd ref="1"/>
    <tag k="landuse" v="forest"/></way>
  <way id="15"><nd ref="5"/><nd ref="6"/><tag k="barrier" v="fence"/></way>
</osm>
"""


@pytest.fixture(scope="module")
def osm_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("osm") / "osm.xml"
    path.write_text(OSM_XML)
    return path


@pytest.fixture(scope="module")
def dem_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 1 m DEM rising 0.2 m per metre eastwards: a contour every 50 m (10 m interval)."""
    import rasterio
    from rasterio.transform import from_origin

    x0, y0 = UTM.transform(LON - 0.006, LAT + 0.004)
    cols, rows = 1100, 900
    elev = (1000 + 0.2 * np.arange(cols, dtype="float32"))[None, :].repeat(rows, axis=0)
    path = tmp_path_factory.mktemp("dem") / "dem.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=from_origin(x0, y0, 1.0, 1.0),
    ) as dst:
        dst.write(elev, 1)
    return path


def test_parse_other_tags() -> None:
    tags = topo.parse_other_tags('"ele"=>"1234","name:en"=>"A \\"B\\"","natural"=>"peak"')
    assert tags == {"ele": "1234", "name:en": 'A "B"', "natural": "peak"}
    assert topo.parse_other_tags(None) == {}
    assert topo.parse_other_tags(float("nan")) == {}  # a missing column value from GDAL


def test_load_osm_classifies_features(osm_path: Path) -> None:
    osm = topo.load_osm(osm_path)
    kinds = sorted(kind for _, kind, _ in osm.lines)
    assert kinds == ["path", "stream", "track"]  # the fence is ignored
    assert {name for _, kind, name in osm.lines if kind == "track"} == {"Old Mine Road"}
    assert sorted(kind for _, kind in osm.areas) == ["forest", "water"]
    ((x, y, kind, name, ele),) = osm.points
    assert (kind, name, ele) == ("peak", "Test Peak", "1234")
    assert abs(x) < 20_037_508 and 4_000_000 < y < 4_300_000  # Web Mercator metres


def test_overpass_query_covers_the_bbox_and_feature_types() -> None:
    query = topo.overpass_query((-117.6, 34.2, -117.5, 34.3))
    assert "34.2,-117.6,34.3,-117.5" in query
    for needle in ("highway", "waterway", "wood", "peak", "(._;>;)"):
        assert needle in query


def test_contours_follow_the_unit_and_mark_index_lines(dem_path: Path) -> None:
    metric = topo.build_contours(dem_path, "m")
    levels = sorted({round(level) for _, level, _ in metric})
    assert levels and all(level % 10 == 0 for level in levels)
    assert {round(level) for _, level, idx in metric if idx} <= {
        lvl for lvl in levels if lvl % 50 == 0
    }
    assert any(idx for _, _, idx in metric) and any(not idx for _, _, idx in metric)
    assert levels[0] >= 1000 and levels[-1] <= 1000 + 0.2 * 1100

    feet = topo.build_contours(dem_path, "ft")
    ft_levels = sorted({round(level) for _, level, _ in feet})
    assert all(level % 40 == 0 for level in ft_levels)
    assert {round(level) for _, level, idx in feet if idx} <= {
        lvl for lvl in ft_levels if lvl % 200 == 0
    }
    # Same terrain, so the highest foot contour sits near the highest metre contour.
    assert abs(ft_levels[-1] - levels[-1] * topo.FEET_PER_METRE) < 40


def test_dashed_splits_a_polyline() -> None:
    dashes = topo._dashed([(0, 0), (100, 0)], 10, 10)
    assert len(dashes) == 5
    assert dashes[0][0] == (0, 0) and dashes[0][-1] == (10.0, 0.0)


def _image(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


@pytest.fixture(scope="module")
def renderer(osm_path: Path, dem_path: Path, tmp_path_factory: pytest.TempPathFactory):
    from canyongis.geopackage.tile_sources import hillshade_geotiff

    shade = hillshade_geotiff(dem_path, tmp_path_factory.mktemp("shade") / "hillshade.tif")
    return topo.TopoRenderer(
        topo.load_osm(osm_path),
        {"m": topo.build_contours(dem_path, "m"), "ft": topo.build_contours(dem_path, "ft")},
        hillshade=shade,
    )


def test_base_tile_is_an_opaque_256_png_with_map_content(renderer) -> None:
    x, y = lonlat_to_tile(LON, LAT, 16)
    img = _image(renderer.base(16, x, y))
    assert img.size == (256, 256) and img.mode == "RGB"
    colours = {c for _, c in img.getcolors(maxcolors=100_000)}
    assert any(
        abs(r - topo.WATER[0]) + abs(g - topo.WATER[1]) + abs(b - topo.WATER[2]) < 12
        for r, g, b in colours
    ), "water should be drawn"
    assert len(colours) > 20


def test_contour_tile_is_transparent_with_lines_and_none_when_empty(renderer) -> None:
    x, y = lonlat_to_tile(LON, LAT, 16)
    img = _image(renderer.contour_tile("m", 16, x, y))
    assert img.mode == "RGBA" and img.size == (256, 256)
    alpha = np.asarray(img.getchannel("A"))
    assert 0 < (alpha > 0).mean() < 0.5
    far_x, far_y = lonlat_to_tile(LON + 1.0, LAT + 1.0, 16)
    assert renderer.contour_tile("m", 16, far_x, far_y) is None


def test_contour_lines_continue_across_a_tile_edge(renderer) -> None:
    """Lines run north-south; the same rows must be inked on both sides of a vertical tile edge."""
    x, y = lonlat_to_tile(LON, LAT, 16)
    left, right = renderer.contour_tile("m", 16, x, y), renderer.contour_tile("m", 16, x + 1, y)
    assert left and right
    a = np.asarray(_image(left).getchannel("A"))[:, -1] > 0
    b = np.asarray(_image(right).getchannel("A"))[:, 0] > 0
    # Lines here are near-vertical, so crossing the edge is rare: compare by inked rows.
    assert a.sum() + b.sum() == 0 or abs(int(a.sum()) - int(b.sum())) <= max(3, int(a.sum()) // 2)


def test_labels_are_decided_once_per_zoom(renderer) -> None:
    first = renderer._labels("contours_m", 16)
    assert first and renderer._labels("contours_m", 16) is first
    assert {label.text for label in first} <= {str(v) for v in range(900, 1300, 50)}
    # Kept labels do not overlap each other.
    res = topo.metres_per_pixel(16)
    boxes = [
        (l.x / res - l.w / 2, l.y / res - l.h / 2, l.x / res + l.w / 2, l.y / res + l.h / 2)
        for l in first
        if abs(l.angle) < 1e-6
    ]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]

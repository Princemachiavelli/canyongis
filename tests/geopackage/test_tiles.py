from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from canyongis.geopackage.schema import create_geopackage
from canyongis.geopackage.tile_build import canyon_bbox_lonlat, plan
from canyongis.geopackage.tile_sources import mbtiles_source
from canyongis.geopackage.tiles import (
    WORLD_HALF_SIZE,
    TileIndex,
    buffer_bbox_lonlat,
    count_tiles,
    lonlat_to_tile,
    tile_bounds_3857,
    tile_bytes_at,
    tiles_for_bbox,
    write_tile_table,
)


def test_lonlat_to_tile_known_values() -> None:
    assert lonlat_to_tile(0.0, 0.0, 1) == (1, 1)
    assert lonlat_to_tile(-180.0, 85.0, 1) == (0, 0)
    assert lonlat_to_tile(-117.57694, 34.2533, 14) == (2840, 6530)
    x, y = lonlat_to_tile(-117.57694, 34.2533, 17)
    assert 0 <= x < 2 ** 17 and 0 <= y < 2 ** 17


def test_tile_bounds_cover_world_at_zoom_zero() -> None:
    bounds = tile_bounds_3857(0, 0, 0)
    assert bounds[0] == pytest.approx(-WORLD_HALF_SIZE)
    assert bounds[1] == pytest.approx(-WORLD_HALF_SIZE)
    assert bounds[2] == pytest.approx(WORLD_HALF_SIZE)
    assert bounds[3] == pytest.approx(WORLD_HALF_SIZE)


def test_tms_row_flips_y() -> None:
    assert TileIndex(2, 0, 0).tms_row == 3
    assert TileIndex(2, 0, 3).tms_row == 0


def test_tiles_for_bbox_contains_the_point() -> None:
    bbox = (-117.6, 34.2, -117.5, 34.3)
    wanted = lonlat_to_tile(-117.57694, 34.2533, 15)
    found = {(t.x, t.y) for t in tiles_for_bbox(bbox, [15])}
    assert wanted in found


def test_count_grows_with_zoom() -> None:
    bbox = (-117.6, 34.2, -117.5, 34.3)
    counts = count_tiles(bbox, [14, 15, 16])
    assert counts[15] > counts[14] and counts[16] > counts[15]


def test_buffer_adds_about_500_metres() -> None:
    west, south, east, north = buffer_bbox_lonlat((-117.6, 34.2, -117.5, 34.3), 500.0)
    assert -117.6 - west == pytest.approx(500 / (111_320 * 0.8265), abs=1e-6)  # lon at 34.25 N
    assert 34.2 - south == pytest.approx(500 / 111_320, abs=1e-6)


def test_write_tile_table_structure_and_skips(tmp_path: Path) -> None:
    gpkg = tmp_path / "canyons.gpkg"
    conn = create_geopackage(gpkg)
    bbox = (-117.6, 34.2, -117.58, 34.22)

    def render(z: int, x: int, y: int) -> bytes | None:
        return None if (z, x, y) == (14, *lonlat_to_tile(-117.59, 34.21, 14)) else f"{z}/{x}/{y}".encode()

    written = write_tile_table(conn, "tiles_test", identifier="Test", description="Test",
                               bbox_lonlat=bbox, zooms=[14, 15], render=render, attribution="x")
    expected = count_tiles(bbox, [14, 15])
    assert written[14] == expected[14] - 1
    assert written[15] == expected[15]

    contents = conn.execute(
        "SELECT data_type, srs_id, description FROM gpkg_contents WHERE table_name='tiles_test'"
    ).fetchone()
    assert contents == ("tiles", 3857, "Test (x)")
    zooms = [r[0] for r in conn.execute(
        "SELECT zoom_level FROM gpkg_tile_matrix WHERE table_name='tiles_test' ORDER BY zoom_level")]
    assert zooms == [14, 15]
    z, x, y = 15, *lonlat_to_tile(-117.59, 34.21, 15)
    assert tile_bytes_at(conn, "tiles_test", z, x, y) == f"{z}/{x}/{y}".encode()
    assert tile_bytes_at(conn, "tiles_test", 14, *lonlat_to_tile(-117.59, 34.21, 14)) is None
    conn.close()


def test_rewrite_replaces_tile_table(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "c.gpkg")
    bbox = (-117.6, 34.2, -117.58, 34.22)
    write_tile_table(conn, "tiles_r", identifier="R", description="R", bbox_lonlat=bbox,
                     zooms=[14], render=lambda z, x, y: b"a")
    write_tile_table(conn, "tiles_r", identifier="R", description="R", bbox_lonlat=bbox,
                     zooms=[14], render=lambda z, x, y: b"b")
    count = conn.execute("SELECT COUNT(*) FROM tiles_r").fetchone()[0]
    assert count == count_tiles(bbox, [14])[14]
    assert conn.execute("SELECT COUNT(*) FROM gpkg_contents WHERE table_name='tiles_r'").fetchone()[0] == 1
    conn.close()


def test_invalid_table_name_rejected(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "c.gpkg")
    with pytest.raises(ValueError):
        write_tile_table(conn, "bad name; drop", identifier="x", description="x",
                         bbox_lonlat=(0, 0, 1, 1), zooms=[1], render=lambda z, x, y: b"")
    conn.close()


def test_mbtiles_source_reads_tms_rows(tmp_path: Path) -> None:
    path = tmp_path / "osm.mbtiles"
    mb = sqlite3.connect(path)
    mb.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    mb.execute("INSERT INTO tiles VALUES (2, 1, 2, ?)", (b"png-bytes",))  # XYZ y = 1
    mb.commit()
    mb.close()
    render = mbtiles_source(path)
    assert render(2, 1, 1) == b"png-bytes"
    assert render(2, 0, 0) is None


def test_plan_on_fixture_counts_every_layer(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[2] / "web" / "canyon-viewer" / "test" / "fixtures" / "lytle.gpkg"
    conn = sqlite3.connect(fixture)
    bbox = buffer_bbox_lonlat(canyon_bbox_lonlat(conn), 500.0)
    conn.close()
    counts = plan(bbox, ["osm", "imagery", "hillshade"])
    assert set(counts) == {"osm", "imagery", "hillshade"}
    assert sum(counts["osm"].values()) > sum(counts["hillshade"].values())

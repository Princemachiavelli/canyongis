"""Terrain (elevation) tiles for the viewer's 3D map: Terrarium PNG built from a DEM."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.geopackage import tile_build, tile_sources
from canyongis.geopackage.geometry import point_blob
from canyongis.geopackage.schema import create_geopackage
from canyongis.geopackage.tiles import TILE_SIZE, ZOOMS_TERRAIN, lonlat_to_tile, tile_bounds_3857

LON, LAT = -117.56, 34.25
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:26911", always_xy=True)
FROM_3857 = Transformer.from_crs("EPSG:3857", "EPSG:26911", always_xy=True)


def _dem(path: Path, *, size: int = 6000, hole: bool = False) -> tuple[float, float]:
    """A 1 m DEM in UTM 11N: a plane rising 0.5 m per m eastwards and 0.2 m per m northwards.
    Returns the plane's origin (west, north edge)."""
    east, north = TO_UTM.transform(LON, LAT)
    west, top = east - size / 2, north + size / 2
    x = np.arange(size, dtype=np.float32)[None, :]
    y = np.arange(size, dtype=np.float32)[:, None]
    elevation = (1000.0 + 0.5 * x + 0.2 * (size - y)).astype(np.float32)
    if hole:
        elevation[:, : size // 2] = -999999.0  # nothing known west of the middle
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=from_origin(west, top, 1.0, 1.0),
        nodata=-999999.0,
    ) as dst:
        dst.write(elevation, 1)
    return west, top


def _expected(west: float, top: float, east: float, north: float, size: int = 6000) -> float:
    return 1000.0 + 0.5 * (east - west) + 0.2 * (size - (top - north))


def test_terrarium_round_trips_to_a_fraction_of_a_millimetre_per_unit() -> None:
    values = np.array(
        [[-86.0, 0.0, 0.25, 1156.04], [2153.28, 4000.5, 8848.86, -430.5]], dtype=np.float32
    )
    values = np.pad(values, ((0, 254), (0, 252)), mode="edge")
    back = tile_sources.decode_terrarium(tile_sources.encode_terrarium(values))
    assert np.abs(back - values).max() <= 1 / 256 + 1e-3  # blue holds 1/256 m steps
    assert back.shape == (TILE_SIZE, TILE_SIZE) and back.dtype == np.float32


def test_a_known_slope_comes_out_at_the_right_height(tmp_path: Path) -> None:
    west, top = _dem(tmp_path / "dem.tif")
    zoom = 14
    x, y = lonlat_to_tile(LON, LAT, zoom)
    png = tile_sources.terrain_source(tmp_path / "dem.tif")(zoom, x, y)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"  # lossless, never JPEG
    elevation = tile_sources.decode_terrarium(png)
    min_x, min_y, max_x, max_y = tile_bounds_3857(zoom, x, y)
    for col, row in ((70, 70), (128, 128), (180, 90), (100, 190)):  # well inside the DEM
        mx = min_x + (col + 0.5) / TILE_SIZE * (max_x - min_x)
        my = max_y - (row + 0.5) / TILE_SIZE * (max_y - min_y)
        east, north = FROM_3857.transform(mx, my)
        assert elevation[row, col] == pytest.approx(_expected(west, top, east, north), abs=1.0), (
            col,
            row,
        )


def test_pixels_the_dem_does_not_cover_are_filled_not_zero(tmp_path: Path) -> None:
    _dem(tmp_path / "dem.tif", hole=True)
    zoom = 14
    x, y = lonlat_to_tile(LON, LAT, zoom)  # the DEM's known half ends in the middle of the tile
    png = tile_sources.terrain_source(tmp_path / "dem.tif")(zoom, x, y)
    assert png is not None
    elevation = tile_sources.decode_terrarium(png)
    assert elevation.min() > 900 and elevation.max() < 5000  # no 0 m (or -32768 m) holes
    assert np.isfinite(elevation).all()


def test_tiles_the_dem_never_touches_are_skipped(tmp_path: Path) -> None:
    _dem(tmp_path / "dem.tif", size=500)
    x, y = lonlat_to_tile(LON + 0.5, LAT + 0.5, 14)
    assert tile_sources.terrain_source(tmp_path / "dem.tif")(14, x, y) is None


def _canyon_file(path: Path) -> Path:
    conn = create_geopackage(path)
    conn.execute(
        "INSERT INTO canyons (ropewiki_title, raw_template_json, source_url, fetched_at, geom)"
        " VALUES ('T', '{}', 'u', 'now', ?)",
        (point_blob(LON, LAT),),
    )
    conn.commit()
    conn.close()
    return path


def test_terrain_goes_into_a_tile_table_the_viewer_can_read(tmp_path: Path) -> None:
    _dem(tmp_path / "dem.tif")
    gpkg = _canyon_file(tmp_path / "c.gpkg")
    tile_build.build_cli(
        [
            str(gpkg),
            "--dem",
            str(tmp_path / "dem.tif"),
            "--terrain",
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    conn = sqlite3.connect(gpkg)
    kind, identifier, description = conn.execute(
        "SELECT data_type, identifier, description FROM gpkg_contents"
        " WHERE table_name = 'tiles_terrain'"
    ).fetchone()
    assert (kind, identifier) == ("tiles", "Elevation") and "USGS 1 m DEM" in description
    zooms = {z for (z,) in conn.execute("SELECT DISTINCT zoom_level FROM tiles_terrain")}
    assert zooms == set(ZOOMS_TERRAIN)
    assert conn.execute(
        "SELECT srs_id FROM gpkg_contents WHERE table_name = 'tiles_terrain'"
    ).fetchone() == (3857,)
    # decoding a stored tile gives plausible heights for the plane (1000 m and up)
    (data,) = conn.execute(
        "SELECT tile_data FROM tiles_terrain WHERE zoom_level = 14 LIMIT 1"
    ).fetchone()
    heights = tile_sources.decode_terrarium(bytes(data))
    assert heights.min() > 1000 and heights.max() < 4000
    # it is only elevation: no picture layers were added
    assert conn.execute(
        "SELECT count(*) FROM gpkg_contents WHERE data_type = 'tiles'"
    ).fetchone() == (1,)


def test_terrain_is_counted_in_the_plan_and_needs_a_dem(tmp_path: Path, capsys) -> None:
    plan = tile_build.plan((LON, LAT, LON + 0.01, LAT + 0.01), ["terrain", "osm"])
    assert set(plan) == {"terrain", "osm"} and set(plan["terrain"]) == set(ZOOMS_TERRAIN)
    gpkg = _canyon_file(tmp_path / "c.gpkg")
    with pytest.raises(SystemExit):
        tile_build.build_cli([str(gpkg), "--terrain"])
    assert "--terrain needs a DEM" in capsys.readouterr().err

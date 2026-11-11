"""Raster tile tables for a canyon GeoPackage (GeoPackage 1.4 tiles, EPSG:3857).

Pure functions and SQL only, so the tile math and table layout can be tested without
GIS libraries. Image encoding and source reading live in ``tile_sources``.

Tile addressing follows the XYZ scheme (y=0 at the north edge) in the public API, and
the GeoPackage TMS row (y=0 at the south edge) in the database.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

WEB_MERCATOR_SRS_ID = 3857
TILE_SIZE = 256
# Half the circumference of the Web Mercator world, in metres.
WORLD_HALF_SIZE = 20037508.342789244

WEB_MERCATOR_WKT = (
    'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]],PROJECTION["Mercator_1SP"],'
    'PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],'
    'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
    'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],'
    'AXIS["Northing",NORTH],AUTHORITY["EPSG","3857"]]'
)

# Zoom levels used for the canyon tile layers.
ZOOMS_MAP = range(13, 19)
ZOOMS_IMAGERY = range(14, 19)
ZOOMS_HILLSHADE = range(14, 18)
ZOOMS_CONTOURS = range(14, 18)
# Elevation for the 3D map. Beyond the last zoom the viewer stretches the data; 16 is about 2.4 m
# per pixel at 34 N, close to the 1 m DEM's useful detail at 256 px a tile.
ZOOMS_TERRAIN = range(11, 17)

# ---- when a built layer is out of date ----------------------------------------------------
# Each tile table is stamped with the version of the code that built it and when. A client (or
# the server's own artifact) is out of date for a layer when it has none, when its version is
# older than LAYER_VERSIONS says, or when it is older than TILE_MAX_AGE. Bump a layer's version
# whenever its zoom range, source data, rendering or encoding changes, so every copy of it gets
# rebuilt. A table not listed here counts as version 1.
LAYER_VERSIONS: dict[str, int] = {
    "tiles_osm": 1,
    "tiles_imagery": 1,
    "tiles_hillshade": 1,
    "tiles_contours_m": 1,
    "tiles_contours_ft": 1,
    "tiles_terrain": 1,
}
TILE_MAX_AGE = timedelta(days=365)


def layer_version(table: str) -> int:
    return LAYER_VERSIONS.get(table, 1)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def layer_staleness(
    version: int | None, built_at: str | None, table: str, now: datetime | None = None
) -> str | None:
    """Why a held layer is out of date: ``"version"``, ``"age"`` or ``"unknown"`` (no build
    time to judge by), else None. A layer that is not held at all is "missing" to the caller."""
    if version is None or version < layer_version(table):
        return "version"
    try:
        built = datetime.fromisoformat((built_at or "").replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if built.tzinfo is None:
        built = built.replace(tzinfo=UTC)
    return "age" if (now or datetime.now(UTC)) - built > TILE_MAX_AGE else None


def stamp_tile_layer(conn: sqlite3.Connection, table: str) -> None:
    """Record that ``table`` was just built, by this version of the code."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tile_stamps ("
        " layer TEXT PRIMARY KEY, version INTEGER NOT NULL, built_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO tile_stamps (layer, version, built_at) VALUES (?,?,?)",
        (table, layer_version(table), utc_now_iso()),
    )


def read_tile_stamps(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """The tile layers a file holds: {table: {version, built_at}}. A layer from before stamps
    existed counts as version 1, built when its gpkg_contents row last changed."""
    layers: dict[str, dict[str, object]] = {
        t: {"version": 1, "built_at": changed}
        for t, changed in conn.execute(
            "SELECT table_name, last_change FROM gpkg_contents WHERE data_type = 'tiles'"
        )
    }
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tile_stamps'"
    ).fetchone()
    if has:
        for table, version, built_at in conn.execute(
            "SELECT layer, version, built_at FROM tile_stamps"
        ):
            if table in layers:
                layers[table] = {"version": version, "built_at": built_at}
    return layers


@dataclass(frozen=True)
class TileIndex:
    zoom: int
    x: int
    y: int  # XYZ row, 0 at the north edge

    @property
    def tms_row(self) -> int:
        """GeoPackage ``tile_row``: 0 at the south edge."""
        return (1 << self.zoom) - 1 - self.y


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.0511287798), -85.0511287798))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds_3857(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of a tile in EPSG:3857 metres."""
    size = 2 * WORLD_HALF_SIZE / (1 << zoom)
    min_x = -WORLD_HALF_SIZE + x * size
    max_y = WORLD_HALF_SIZE - y * size
    return min_x, max_y - size, min_x + size, max_y


def tiles_for_bbox(
    bbox_lonlat: tuple[float, float, float, float], zooms: Iterable[int]
) -> Iterator[TileIndex]:
    """Every tile intersecting a WGS84 bbox ``(west, south, east, north)``, per zoom."""
    west, south, east, north = bbox_lonlat
    for zoom in zooms:
        x0, y1 = lonlat_to_tile(west, south, zoom)
        x1, y0 = lonlat_to_tile(east, north, zoom)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield TileIndex(zoom, x, y)


def count_tiles(
    bbox_lonlat: tuple[float, float, float, float], zooms: Iterable[int]
) -> dict[int, int]:
    counts: dict[int, int] = {}
    for tile in tiles_for_bbox(bbox_lonlat, zooms):
        counts[tile.zoom] = counts.get(tile.zoom, 0) + 1
    return counts


def buffer_bbox_lonlat(
    bbox_lonlat: tuple[float, float, float, float], buffer_m: float
) -> tuple[float, float, float, float]:
    """Grow a WGS84 bbox by ``buffer_m`` metres on each side (small-area approximation)."""
    west, south, east, north = bbox_lonlat
    dlat = buffer_m / 111_320.0
    mid_lat = math.radians((south + north) / 2.0)
    dlon = buffer_m / (111_320.0 * max(math.cos(mid_lat), 1e-6))
    return west - dlon, max(south - dlat, -85.0), east + dlon, min(north + dlat, 85.0)


def bbox_3857_of_lonlat(
    bbox_lonlat: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    west, south, east, north = bbox_lonlat

    def merc(lon: float, lat: float) -> tuple[float, float]:
        lat = max(min(lat, 85.0511287798), -85.0511287798)
        return (
            lon * WORLD_HALF_SIZE / 180.0,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * WORLD_HALF_SIZE / math.pi,
        )

    x0, y0 = merc(west, south)
    x1, y1 = merc(east, north)
    return x0, y0, x1, y1


# A render callback returns encoded tile bytes, or None for "no data here".
Render = Callable[[int, int, int], bytes | None]


def ensure_web_mercator_srs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
        (
            "WGS 84 / Pseudo-Mercator",
            WEB_MERCATOR_SRS_ID,
            "EPSG",
            WEB_MERCATOR_SRS_ID,
            WEB_MERCATOR_WKT,
            "Web Mercator, EPSG:3857",
        ),
    )


def write_tile_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    identifier: str,
    description: str,
    bbox_lonlat: tuple[float, float, float, float],
    zooms: Iterable[int],
    render: Render,
    attribution: str | None = None,
) -> dict[int, int]:
    """Create a GeoPackage tile table and fill it with tiles from ``render``.

    Only tiles that intersect ``bbox_lonlat`` are requested; tiles where ``render``
    returns ``None`` are skipped. Returns the number of tiles written per zoom level.
    Re-running replaces the table.
    """
    zooms = sorted(set(zooms))
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name: {table!r}")
    ensure_web_mercator_srs(conn)
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"""
        CREATE TABLE {table} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zoom_level  INTEGER NOT NULL,
            tile_column INTEGER NOT NULL,
            tile_row    INTEGER NOT NULL,
            tile_data   BLOB NOT NULL,
            UNIQUE (zoom_level, tile_column, tile_row)
        )""")
    conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (table,))
    conn.execute("DELETE FROM gpkg_tile_matrix WHERE table_name = ?", (table,))
    conn.execute("DELETE FROM gpkg_tile_matrix_set WHERE table_name = ?", (table,))

    extent = bbox_3857_of_lonlat(bbox_lonlat)
    description_text = description if attribution is None else f"{description} ({attribution})"
    conn.execute(
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description,"
        " min_x, min_y, max_x, max_y, srs_id) VALUES (?, 'tiles', ?, ?, ?, ?, ?, ?, ?)",
        (table, identifier, description_text, *extent, WEB_MERCATOR_SRS_ID),
    )
    conn.execute(
        "INSERT INTO gpkg_tile_matrix_set (table_name, srs_id, min_x, min_y, max_x, max_y)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            table,
            WEB_MERCATOR_SRS_ID,
            -WORLD_HALF_SIZE,
            -WORLD_HALF_SIZE,
            WORLD_HALF_SIZE,
            WORLD_HALF_SIZE,
        ),
    )
    for zoom in zooms:
        pixel = 2 * WORLD_HALF_SIZE / (TILE_SIZE * (1 << zoom))
        conn.execute(
            "INSERT INTO gpkg_tile_matrix VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (table, zoom, 1 << zoom, 1 << zoom, TILE_SIZE, TILE_SIZE, pixel, pixel),
        )

    written: dict[int, int] = {}
    for zoom in zooms:
        for tile in tiles_for_bbox(bbox_lonlat, [zoom]):
            data = render(tile.zoom, tile.x, tile.y)
            if data is None:
                continue
            conn.execute(
                f"INSERT INTO {table} (zoom_level, tile_column, tile_row, tile_data)"
                " VALUES (?, ?, ?, ?)",
                (tile.zoom, tile.x, tile.tms_row, sqlite3.Binary(data)),
            )
            written[zoom] = written.get(zoom, 0) + 1
    stamp_tile_layer(conn, table)
    conn.commit()
    return written


def tile_bytes_at(conn: sqlite3.Connection, table: str, zoom: int, x: int, y: int) -> bytes | None:
    """Read one tile by XYZ address (used by tests and simple readers)."""
    tile = TileIndex(zoom, x, y)
    row = conn.execute(
        f"SELECT tile_data FROM {table} WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
        (zoom, x, tile.tms_row),
    ).fetchone()
    return bytes(row[0]) if row else None

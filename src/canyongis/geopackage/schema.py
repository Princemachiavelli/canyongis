"""GeoPackage schema for canyon data (version 5).

The GeoPackage is an intermediate, portable copy of the canonical store. Its
tables and types are chosen to map cleanly onto PostGIS later:

* Feature tables (spatial, one geometry column ``geom``, EPSG:4326):
  ``canyons`` (point), ``routes`` (line), ``waypoints`` (point), ``rappels`` (point).
* Attribute tables (no geometry): ``conditions`` (one row per condition report, dated
  by ``trip_date``) with ``condition_rappel_notes`` and ``condition_photos`` children,
  ``ratings``, ``beta`` (wiki, plain and HTML text), ``links``, ``kml_files``, and the
  weather ``forecasts`` / ``forecast_days``.
* Run tables written by the canyon companion app (``runs`` line, ``run_events`` and
  ``run_fixes`` points). They are keyed by canyon title, not ``canyon_id``.
* ``schema_meta`` holds the schema version and generator.

Version 4 added what the KML itself says: ``routes.color`` (``#rrggbb``), ``waypoints.description``
and ``rappels.kml_name`` / ``kml_description`` (the node's own name, e.g. ``R1-R5``, and text).

Natural keys (``canyons.ropewiki_title``) are unique, and every child row carries
an integer ``canyon_id`` foreign key, so loading into PostgreSQL is a row-for-row copy.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .geometry import WGS84_SRS_ID, blob_envelope

SCHEMA_VERSION = "5"
GENERATOR = "canyongis-geopackage"
# GeoPackage 1.4 application_id and user_version (see OGC 12-128r18).
APPLICATION_ID = 0x47504B47  # 'GPKG'
USER_VERSION = 10400

WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
    'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],'
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]'
)

# (table, identifier, description, geometry type or None for attribute tables)
FEATURE_TABLES: tuple[tuple[str, str, str, str], ...] = (
    ("canyons", "Canyons", "One row per canyon; location point", "POINT"),
    ("routes", "Routes", "Approach, descent, exit and road tracks from KML", "LINESTRING"),
    ("waypoints", "Waypoints", "Parking, start, end and other KML points", "POINT"),
    ("rappels", "Rappels", "Rappel anchor points, labelled R1, R2, ...", "POINT"),
    # Written by the canyon companion app when it saves a GeoPackage (schema version 3).
    ("runs", "Runs", "Descents recorded by the companion app (track from GPS fixes)", "LINESTRING"),
    ("run_events", "Run events", "Start, next, back, jump, mark and finish events", "POINT"),
    ("run_fixes", "Run GPS fixes", "GPS positions logged during a run", "POINT"),
)

ATTRIBUTE_TABLES: tuple[tuple[str, str], ...] = (
    ("conditions", "Condition reports, from Conditions: pages"),
    ("ratings", "Ratings, from Votes: pages"),
    ("beta", "Prose sections of each canyon page: wiki, plain and HTML text"),
    ("links", "External links, from the Beta sites section"),
    ("kml_files", "Source KML files and their checksums"),
    ("forecasts", "Weather forecast fetched for each canyon, with its fetch time"),
    ("forecast_days", "Daily forecast values"),
    ("forecast_hours", "Hourly forecast values"),
    (
        "section_stamps",
        "Content fingerprint of each data section, so updates can tell what changed",
    ),
)

_DDL = """
CREATE TABLE canyons (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ropewiki_title       TEXT NOT NULL UNIQUE,
    ropewiki_page_id     INTEGER,
    also_known_as        TEXT,
    region               TEXT,
    technical_rating     INTEGER,
    water_rating         TEXT,
    time_rating          TEXT,
    number_of_rappels    INTEGER,
    longest_rappel_m     REAL,
    hike_length_km       REAL,
    approach_gain_m      REAL,
    exit_gain_m          REAL,
    fastest_time_h       REAL,
    slowest_time_h       REAL,
    permits              TEXT,
    best_season          TEXT,
    vehicle              TEXT,
    shuttle              INTEGER,
    explored             INTEGER,
    raw_template_json    TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    source_revision_id   INTEGER,
    fetched_at           TEXT NOT NULL,
    license_name         TEXT,
    license_url          TEXT,
    anonymous_authors    INTEGER,
    geom                 BLOB
);

CREATE TABLE routes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('approach', 'descent', 'exit', 'road', 'unknown')),
    name         TEXT,
    color        TEXT,
    kml_file_id  INTEGER REFERENCES kml_files(id) ON DELETE SET NULL,
    length_m     REAL,
    geom         BLOB
);

CREATE TABLE waypoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    name         TEXT,
    description  TEXT,
    kml_file_id  INTEGER REFERENCES kml_files(id) ON DELETE SET NULL,
    geom         BLOB
);

CREATE TABLE rappels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    sequence     INTEGER NOT NULL,
    label        TEXT NOT NULL,
    height_m     REAL,
    anchor_text  TEXT,
    notes        TEXT,
    kml_name        TEXT,
    kml_description TEXT,
    kml_file_id  INTEGER REFERENCES kml_files(id) ON DELETE SET NULL,
    geom         BLOB,
    UNIQUE (canyon_id, sequence)
);

CREATE TABLE conditions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id             INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    source_title          TEXT NOT NULL UNIQUE,
    trip_date             TEXT,
    trip_date_source      TEXT,
    reported_by           TEXT,
    reported_at           TEXT,
    revised_at            TEXT,
    revision_id           INTEGER,
    location_text         TEXT,
    quality_code          INTEGER,
    quality_label         TEXT,
    water_code            INTEGER,
    water_label           TEXT,
    water_note            TEXT,
    wetsuit_code          INTEGER,
    wetsuit_label         TEXT,
    wetsuit_note          TEXT,
    difficulty_code       INTEGER,
    difficulty_label      TEXT,
    difficulty_note       TEXT,
    team_size             INTEGER,
    team_time_h           REAL,
    team_min_experience   TEXT,
    team_max_experience   TEXT,
    general_notes         TEXT,
    source_url            TEXT NOT NULL,
    raw_template_json     TEXT NOT NULL
);
CREATE TABLE condition_rappel_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    INTEGER NOT NULL REFERENCES conditions(id) ON DELETE CASCADE,
    rappel_label TEXT NOT NULL,
    note         TEXT NOT NULL
);
CREATE TABLE condition_photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    INTEGER NOT NULL REFERENCES conditions(id) ON DELETE CASCADE,
    sequence     INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    UNIQUE (report_id, sequence)
);

CREATE TABLE ratings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    source_title TEXT NOT NULL UNIQUE,
    rater        TEXT,
    rating       INTEGER
);

CREATE TABLE beta (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    heading      TEXT NOT NULL,
    body_wiki    TEXT NOT NULL,
    body_text    TEXT NOT NULL,
    body_html    TEXT NOT NULL,
    UNIQUE (canyon_id, position)
);

CREATE TABLE links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id    INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    label        TEXT,
    url          TEXT NOT NULL,
    source_name  TEXT,
    UNIQUE (canyon_id, url)
);

CREATE TABLE kml_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id     INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    file_title    TEXT NOT NULL,
    url           TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    UNIQUE (canyon_id, file_title)
);

CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Tables added in schema version 3. Runs are keyed by the canyon's Ropewiki title and rappels by
# sequence number (not by ``canyons.id``): re-running the converter replaces a canyon row, and a
# foreign key would delete the runs recorded for it. The statements are idempotent so an older
# file can be upgraded in place; web/canyon-viewer/gpkg-write.js carries an identical copy
# (tests/geopackage/test_schema_sync.py checks they agree).
_DDL_V3 = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid     TEXT NOT NULL UNIQUE,
    canyon_title TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    next_index   INTEGER,
    source       TEXT,
    geom         BLOB
);
CREATE TABLE IF NOT EXISTS run_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid        TEXT NOT NULL REFERENCES runs(run_uuid) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    t               TEXT NOT NULL,
    type            TEXT NOT NULL,
    rappel_sequence INTEGER,
    acc             REAL,
    geom            BLOB,
    UNIQUE (run_uuid, seq)
);
CREATE TABLE IF NOT EXISTS run_fixes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL REFERENCES runs(run_uuid) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    t        TEXT NOT NULL,
    acc      REAL,
    geom     BLOB,
    UNIQUE (run_uuid, seq)
);
CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id   INTEGER NOT NULL UNIQUE REFERENCES canyons(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    elevation_m REAL,
    timezone    TEXT
);
CREATE TABLE IF NOT EXISTS forecast_days (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id     INTEGER NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    weather_code    INTEGER,
    temp_max_c      REAL,
    temp_min_c      REAL,
    precip_mm       REAL,
    precip_prob_pct INTEGER,
    wind_max_kmh    REAL,
    gust_max_kmh    REAL,
    sunrise         TEXT,
    sunset          TEXT,
    UNIQUE (forecast_id, date)
);
CREATE INDEX IF NOT EXISTS idx_runs_canyon ON runs (canyon_title);
"""

# Added in schema version 5: for each canyon, the Ropewiki page authors (attribution) and a
# fingerprint of every section of its data
# (see pgstore.SECTIONS), made by the server when it exported the file. A client compares them
# with the server's to see which sections are out of date. Mirrored in
# web/canyon-viewer/gpkg-write.js (tests/geopackage/test_forecast.py checks they agree).
_DDL_V5 = """
CREATE TABLE IF NOT EXISTS section_stamps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id  INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    section    TEXT NOT NULL,
    digest     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (canyon_id, section)
);
CREATE TABLE IF NOT EXISTS forecast_hours (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id     INTEGER NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    time            TEXT NOT NULL,
    weather_code    INTEGER,
    temp_c          REAL,
    precip_mm       REAL,
    precip_prob_pct INTEGER,
    wind_kmh        REAL,
    gust_kmh        REAL,
    UNIQUE (forecast_id, time)
);
CREATE TABLE IF NOT EXISTS authors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    UNIQUE (canyon_id, name)
);
"""

_INDEXES = """
CREATE INDEX idx_routes_canyon ON routes (canyon_id);
CREATE INDEX idx_waypoints_canyon ON waypoints (canyon_id);
CREATE INDEX idx_rappels_canyon ON rappels (canyon_id);
CREATE INDEX idx_conditions_canyon_date ON conditions (canyon_id, trip_date);
CREATE INDEX idx_condition_notes_report ON condition_rappel_notes (report_id);
CREATE INDEX idx_condition_photos_report ON condition_photos (report_id);
CREATE INDEX idx_ratings_canyon ON ratings (canyon_id);
CREATE INDEX idx_beta_sections_canyon ON beta (canyon_id);
CREATE INDEX idx_links_canyon ON links (canyon_id);
CREATE INDEX idx_kml_canyon ON kml_files (canyon_id);
"""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def create_geopackage(path: str | Path) -> sqlite3.Connection:
    """Create an empty canyon GeoPackage at ``path`` (fails if it exists)."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.executescript(_gpkg_core_ddl())
    conn.executescript(_DDL)
    conn.executescript(_INDEXES)
    conn.executescript(_DDL_V3)
    conn.executescript(_DDL_V5)
    _register_tables(conn)
    conn.executemany(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        [("schema_version", SCHEMA_VERSION), ("generator", GENERATOR), ("created_at", _now_iso())],
    )
    conn.commit()
    return conn


# Columns added in version 4: what the KML itself says (line colour, node name and description).
_COLUMNS_V4 = (
    ("routes", "color", "TEXT"),
    ("waypoints", "description", "TEXT"),
    ("rappels", "kml_name", "TEXT"),
    ("rappels", "kml_description", "TEXT"),
    # attribution (schema version 5)
    ("canyons", "license_name", "TEXT"),
    ("canyons", "license_url", "TEXT"),
    ("canyons", "anonymous_authors", "INTEGER"),
)


def upgrade_geopackage(conn: sqlite3.Connection) -> None:
    """Bring a file built by an older version up to the current schema (adds what is missing:
    the version 3 tables for runs and forecasts, and the version 4 KML columns)."""
    conn.executescript(_DDL_V3)
    conn.executescript(_DDL_V5)
    for table, column, kind in _COLUMNS_V4:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    _register_tables(conn)
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def open_geopackage(path: str | Path) -> sqlite3.Connection:
    """Open an existing canyon GeoPackage; never creates a file or accepts an empty one."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    conn = sqlite3.connect(path)
    has_canyons = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'canyons'"
    ).fetchone()
    if not has_canyons:
        conn.close()
        raise ValueError(
            f"{path} is not a canyon GeoPackage (no 'canyons' table; empty or incomplete file?)"
        )
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _gpkg_core_ddl() -> str:
    return """
CREATE TABLE gpkg_spatial_ref_sys (
    srs_name                 TEXT NOT NULL,
    srs_id                   INTEGER PRIMARY KEY,
    organization             TEXT NOT NULL,
    organization_coordsys_id INTEGER NOT NULL,
    definition               TEXT NOT NULL,
    description              TEXT
);
CREATE TABLE gpkg_contents (
    table_name  TEXT NOT NULL PRIMARY KEY,
    data_type   TEXT NOT NULL,
    identifier  TEXT UNIQUE,
    description TEXT DEFAULT '',
    last_change TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    min_x       DOUBLE,
    min_y       DOUBLE,
    max_x       DOUBLE,
    max_y       DOUBLE,
    srs_id      INTEGER REFERENCES gpkg_spatial_ref_sys(srs_id)
);
CREATE TABLE gpkg_tile_matrix_set (
    table_name TEXT NOT NULL PRIMARY KEY,
    srs_id     INTEGER NOT NULL REFERENCES gpkg_spatial_ref_sys(srs_id),
    min_x      DOUBLE NOT NULL,
    min_y      DOUBLE NOT NULL,
    max_x      DOUBLE NOT NULL,
    max_y      DOUBLE NOT NULL
);
CREATE TABLE gpkg_tile_matrix (
    table_name    TEXT NOT NULL,
    zoom_level    INTEGER NOT NULL,
    matrix_width  INTEGER NOT NULL,
    matrix_height INTEGER NOT NULL,
    tile_width    INTEGER NOT NULL,
    tile_height   INTEGER NOT NULL,
    pixel_x_size  DOUBLE NOT NULL,
    pixel_y_size  DOUBLE NOT NULL,
    CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level)
);
CREATE TABLE gpkg_geometry_columns (
    table_name         TEXT NOT NULL,
    column_name        TEXT NOT NULL,
    geometry_type_name TEXT NOT NULL,
    srs_id             INTEGER NOT NULL REFERENCES gpkg_spatial_ref_sys(srs_id),
    z                  TINYINT NOT NULL,
    m                  TINYINT NOT NULL,
    CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name)
);
INSERT INTO gpkg_spatial_ref_sys VALUES
    ('Undefined cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined cartesian SRS'),
    ('Undefined geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined geographic SRS');
INSERT INTO gpkg_spatial_ref_sys VALUES
    ('WGS 84 geodetic', 4326, 'EPSG', 4326, '{wkt}', 'longitude/latitude WGS 84');
""".replace("{wkt}", WGS84_WKT)


def _register_tables(conn: sqlite3.Connection) -> None:
    for table, identifier, description, geom_type in FEATURE_TABLES:
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_contents"
            " (table_name, data_type, identifier, description, srs_id)"
            " VALUES (?, 'features', ?, ?, ?)",
            (table, identifier, description, WGS84_SRS_ID),
        )
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)",
            (table, geom_type, WGS84_SRS_ID),
        )
    for table, description in ATTRIBUTE_TABLES:
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description)"
            " VALUES (?, 'attributes', ?, ?)",
            (table, table, description),
        )
    conn.execute(
        "INSERT OR IGNORE INTO gpkg_contents"
        " (table_name, data_type, identifier, description, srs_id)"
        " VALUES ('schema_meta', 'attributes', 'schema_meta', 'Schema version', NULL)"
    )


def refresh_extents(conn: sqlite3.Connection) -> None:
    """Recompute ``gpkg_contents`` bounding boxes and ``last_change`` for feature tables."""
    stamp = _now_iso()
    for table, *_ in FEATURE_TABLES:
        boxes = [
            blob_envelope(blob)
            for (blob,) in conn.execute(f"SELECT geom FROM {table} WHERE geom IS NOT NULL")
        ]
        if boxes:
            min_x = min(b[0] for b in boxes)
            min_y = min(b[1] for b in boxes)
            max_x = max(b[2] for b in boxes)
            max_y = max(b[3] for b in boxes)
        else:
            min_x = min_y = max_x = max_y = None
        conn.execute(
            "UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, last_change=?"
            " WHERE table_name=?",
            (min_x, min_y, max_x, max_y, stamp, table),
        )
    conn.commit()

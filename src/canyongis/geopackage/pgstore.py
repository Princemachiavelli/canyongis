"""Canyon data in PostGIS, with GeoPackage as an import and export format.

The ``canyon`` schema (Alembic migration 0006) has the same tables and columns as the
GeoPackage, so both directions are a table-driven copy:

* ``load_geopackage``: GeoPackage rows into PostGIS. A canyon is upserted by Ropewiki title
  (its id stays the same), its child rows are replaced, and a forecast is only replaced by a
  newer one.
* ``export_geopackage``: PostGIS rows into a new GeoPackage, optionally on top of a file that
  already holds map tiles (the tile tables are kept, the vector tables are rewritten).

Ids are not preserved across the copy; foreign keys are remapped. Geometry is converted
between GeoPackage binary and WKB. ``refresh_forecast`` keeps the forecast current inside the
database, so an export never needs an old file to be rebuilt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from ..io import log
from .forecast import DAILY_VARIABLES, HOURLY_VARIABLES, fetch_forecast
from .geometry import WGS84_SRS_ID, _gpkg_header
from .schema import (
    SCHEMA_VERSION,
    create_geopackage,
    open_geopackage,
    refresh_extents,
    upgrade_geopackage,
)

SCHEMA = "canyon"
FORECAST_MAX_AGE = timedelta(hours=3)


@dataclass(frozen=True)
class TableSpec:
    name: str
    # (column, parent table): the first one scopes the table to its canyon / report / forecast.
    parents: tuple[tuple[str, str], ...] = ()
    geom: str | None = None


_CANYON = ("canyon_id", "canyons")
_KML = ("kml_file_id", "kml_files")

# Parents before children.
TABLES: tuple[TableSpec, ...] = (
    TableSpec("canyons", geom="geom"),
    TableSpec("kml_files", (_CANYON,)),
    TableSpec("routes", (_CANYON, _KML), geom="geom"),
    TableSpec("waypoints", (_CANYON, _KML), geom="geom"),
    TableSpec("rappels", (_CANYON, _KML), geom="geom"),
    TableSpec("conditions", (_CANYON,)),
    TableSpec("condition_rappel_notes", (("report_id", "conditions"),)),
    TableSpec("condition_photos", (("report_id", "conditions"),)),
    TableSpec("ratings", (_CANYON,)),
    TableSpec("beta", (_CANYON,)),
    TableSpec("links", (_CANYON,)),
    TableSpec("authors", (_CANYON,)),
    TableSpec("forecasts", (_CANYON,)),
    TableSpec("forecast_days", (("forecast_id", "forecasts"),)),
    TableSpec("forecast_hours", (("forecast_id", "forecasts"),)),
    TableSpec("section_stamps", (_CANYON,)),
)
# Tables the server computes itself: never copied in from a file.
_LOAD_SKIPS = ("section_stamps",)
_BY_NAME = {t.name: t for t in TABLES}
# Child tables replaced wholesale when a canyon is re-ingested (forecasts are handled apart).
_REPLACED_ON_INGEST = (
    "routes",
    "waypoints",
    "rappels",
    "kml_files",
    "conditions",
    "ratings",
    "beta",
    "links",
)
_KEEP_WHEN_NULL = ("license_name", "license_url", "anonymous_authors")
_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


# -----------------------------------------------------------------------------
# geometry
# -----------------------------------------------------------------------------


def gpkg_blob_to_wkb(blob: bytes | None) -> bytes | None:
    """The WKB inside a GeoPackage geometry blob (None for NULL or an empty geometry)."""
    if blob is None:
        return None
    blob = bytes(blob)
    if blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry blob")
    flags = blob[3]
    if flags & 0b0001_0000:  # empty geometry
        return None
    envelope = (flags >> 1) & 0b111
    if envelope not in _ENVELOPE_BYTES:
        raise ValueError(f"unknown GeoPackage envelope indicator {envelope}")
    return blob[8 + _ENVELOPE_BYTES[envelope] :]


def wkb_to_gpkg_blob(wkb: bytes | None, srs_id: int = WGS84_SRS_ID) -> bytes | None:
    return None if wkb is None else _gpkg_header(srs_id) + bytes(wkb)


# -----------------------------------------------------------------------------
# connection and small queries
# -----------------------------------------------------------------------------


def connect(config: Any, *, autocommit: bool = True) -> psycopg.Connection:
    """Open a connection from a ``canyongis.web.db.PostgresConfig``.

    Autocommit by default: the functions here open their own transactions, so nothing is held
    open between steps (a map build can take hours).
    """
    return psycopg.connect(**config.connect_kwargs(), autocommit=autocommit)


def schema_ready(pg: psycopg.Connection) -> bool:
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('canyon.canyons') IS NOT NULL")
        return bool(cur.fetchone()[0])


def _pg_columns(pg: psycopg.Connection, table: str) -> list[str]:
    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (SCHEMA, table),
        )
        return [r[0] for r in cur.fetchall()]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _sqlite_has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _ident(table: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table))


def canyon_fetched_at(pg: psycopg.Connection, title: str) -> datetime | None:
    """When the canyon's Ropewiki data was last ingested, or None if it is not stored."""
    with pg.cursor() as cur:
        cur.execute("SELECT fetched_at FROM canyon.canyons WHERE ropewiki_title = %s", (title,))
        row = cur.fetchone()
    return _parse_time(row[0]) if row else None


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -----------------------------------------------------------------------------
# GeoPackage -> PostGIS
# -----------------------------------------------------------------------------


def load_geopackage(pg: psycopg.Connection, path: str | Path) -> list[str]:
    """Copy every canyon in a GeoPackage into PostGIS in one transaction.

    Returns the canyon titles loaded. Re-loading a canyon keeps its id, replaces its child
    rows and keeps its forecast unless the file's is newer.
    """
    src = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        maps: dict[str, dict[int, int]] = {t.name: {} for t in TABLES}
        titles: list[str] = []
        with pg.transaction():
            for spec in TABLES:
                if spec.name in _LOAD_SKIPS or not _sqlite_has_table(src, spec.name):
                    continue
                pg_cols = set(_pg_columns(pg, spec.name))
                cols = [c for c in _sqlite_columns(src, spec.name) if c in pg_cols and c != "id"]
                rows = src.execute(f"SELECT * FROM {spec.name} ORDER BY id").fetchall()
                if spec.name == "forecasts":
                    rows = _newer_forecasts(pg, src, rows, maps["canyons"])
                if spec.name == "authors":
                    _drop_authors_being_replaced(pg, rows, maps["canyons"])
                for row in rows:
                    new_id = _insert_row(pg, spec, cols, row, maps)
                    if new_id is None:
                        continue
                    maps[spec.name][row["id"]] = new_id
                    if spec.name == "canyons":
                        titles.append(row["ropewiki_title"])
                        _clear_children(pg, new_id)
            for canyon_id in maps["canyons"].values():
                refresh_stamps(pg, canyon_id)
        return titles
    finally:
        src.close()


def _clear_children(pg: psycopg.Connection, canyon_id: int) -> None:
    with pg.cursor() as cur:
        for table in _REPLACED_ON_INGEST:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE canyon_id = %s").format(_ident(table)), (canyon_id,)
            )


def _drop_authors_being_replaced(pg, rows, canyon_map) -> None:
    """A canyon's author list is replaced only if the file has one (a failed fetch leaves none)."""
    ids = sorted({canyon_map[r["canyon_id"]] for r in rows if r["canyon_id"] in canyon_map})
    if ids:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM canyon.authors WHERE canyon_id = ANY(%s)", (ids,))


def _newer_forecasts(pg, src, rows, canyon_map):
    """Only the file's forecasts that are newer than the one already stored (or new)."""
    keep = []
    for row in rows:
        canyon_id = canyon_map.get(row["canyon_id"])
        if canyon_id is None:
            continue
        with pg.cursor() as cur:
            cur.execute(
                "SELECT id, fetched_at FROM canyon.forecasts WHERE canyon_id = %s", (canyon_id,)
            )
            existing = cur.fetchone()
            if existing is None:
                keep.append(row)
                continue
            new, old = _parse_time(row["fetched_at"]), _parse_time(existing[1])
            if new and (old is None or new > old):
                cur.execute("DELETE FROM canyon.forecasts WHERE id = %s", (existing[0],))
                keep.append(row)
    return keep


def _insert_row(pg, spec: TableSpec, cols: list[str], row, maps) -> int | None:
    values: list[Any] = []
    placeholders: list[sql.Composable] = []
    for col in cols:
        value = row[col]
        for fk_col, parent in spec.parents:
            if col == fk_col and value is not None:
                value = maps[parent].get(value)
                if value is None and fk_col != "kml_file_id":
                    return None  # the parent was not loaded (e.g. an older forecast)
        if col == spec.geom:
            values.append(gpkg_blob_to_wkb(value))
            placeholders.append(sql.SQL("ST_SetSRID(ST_GeomFromWKB(%s), 4326)"))
        else:
            values.append(value)
            placeholders.append(sql.SQL("%s"))
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        _ident(spec.name),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(placeholders),
    )
    if spec.name == "canyons":
        updates = [c for c in cols if c != "ropewiki_title"]
        # Attribution is kept when this ingest could not fetch it (the file has NULLs).
        keep = _KEEP_WHEN_NULL
        query += sql.SQL(" ON CONFLICT (ropewiki_title) DO UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{c} = COALESCE(EXCLUDED.{c}, {t}.{c})").format(
                    c=sql.Identifier(c), t=_ident("canyons")
                )
                if c in keep
                else sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                for c in updates
            )
        )
    query += sql.SQL(" RETURNING id")
    with pg.cursor() as cur:
        cur.execute(query, values)
        return cur.fetchone()[0]


# -----------------------------------------------------------------------------
# PostGIS -> GeoPackage
# -----------------------------------------------------------------------------


def export_geopackage(
    pg: psycopg.Connection,
    titles: Iterable[str],
    out: str | Path,
    *,
    base: str | Path | None = None,
) -> dict[str, int]:
    """Write the given canyons to a GeoPackage at ``out`` (replaced atomically).

    With ``base`` (a GeoPackage that already holds map tile tables) the file is copied and its
    vector tables are rewritten, so the tiles come along without being rebuilt. Returns the
    number of rows written per table.
    """
    out = Path(out)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    if base is not None:
        shutil.copyfile(base, tmp)
        conn = open_geopackage(tmp)
        upgrade_geopackage(conn)
        _clear_vector_tables(conn)
    else:
        conn = create_geopackage(tmp)
    counts: dict[str, int] = {}
    try:
        exported: dict[str, list[int]] = {}
        maps: dict[str, dict[int, int]] = {t.name: {} for t in TABLES}
        for spec in TABLES:
            counts[spec.name] = _export_table(pg, conn, spec, list(titles), exported, maps)
        refresh_extents(conn)
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, out)
    return counts


def _clear_vector_tables(conn: sqlite3.Connection) -> None:
    names = [t.name for t in reversed(TABLES)]
    for name in names:
        conn.execute(f"DELETE FROM {name}")
    marks = ",".join("?" for _ in names)
    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({marks})", names)
    conn.commit()


def _export_table(pg, conn, spec: TableSpec, titles, exported, maps) -> int:
    pg_cols = _pg_columns(pg, spec.name)
    sqlite_cols = set(_sqlite_columns(conn, spec.name))
    cols = [c for c in pg_cols if c in sqlite_cols and c != "id"]
    select = [sql.Identifier("id")] + [
        sql.SQL("ST_AsBinary({}, 'NDR')").format(sql.Identifier(c))
        if c == spec.geom
        else sql.Identifier(c)
        for c in cols
    ]
    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(select), _ident(spec.name))
    params: list[Any]
    if spec.name == "canyons":
        query += sql.SQL(" WHERE ropewiki_title = ANY(%s)")
        params = [titles]
    else:
        scope_col, parent = spec.parents[0]
        query += sql.SQL(" WHERE {} = ANY(%s)").format(sql.Identifier(scope_col))
        params = [exported.get(parent, [])]
    query += sql.SQL(" ORDER BY id")
    with pg.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    insert = f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    ids: list[int] = []
    for row in rows:
        pg_id, values = row[0], list(row[1:])
        for i, col in enumerate(cols):
            if col == spec.geom:
                values[i] = wkb_to_gpkg_blob(values[i])
            for fk_col, parent in spec.parents:
                if col == fk_col and values[i] is not None:
                    values[i] = maps[parent].get(values[i])
        cur_sqlite = conn.execute(insert, values)
        maps[spec.name][pg_id] = cur_sqlite.lastrowid
        ids.append(pg_id)
    exported[spec.name] = ids
    return len(rows)


# -----------------------------------------------------------------------------
# forecasts inside the database
# -----------------------------------------------------------------------------


def store_forecast(pg: psycopg.Connection, canyon_id: int, forecast: dict[str, Any]) -> None:
    """Replace the canyon's forecast (a record from ``forecast.parse_forecast``)."""
    columns = ["date", *DAILY_VARIABLES.values()]
    with pg.transaction(), pg.cursor() as cur:
        cur.execute("DELETE FROM canyon.forecasts WHERE canyon_id = %s", (canyon_id,))
        cur.execute(
            "INSERT INTO canyon.forecasts (canyon_id, source, fetched_at, latitude, longitude,"
            " elevation_m, timezone) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                canyon_id,
                forecast["source"],
                forecast["fetched_at"],
                forecast["latitude"],
                forecast["longitude"],
                forecast["elevation_m"],
                forecast["timezone"],
            ),
        )
        forecast_id = cur.fetchone()[0]
        cur.executemany(
            f"INSERT INTO canyon.forecast_days (forecast_id, {', '.join(columns)})"
            f" VALUES (%s, {', '.join(['%s'] * len(columns))})",
            [(forecast_id, *(day[c] for c in columns)) for day in forecast["days"]],
        )
        hour_columns = ["time", *HOURLY_VARIABLES.values()]
        cur.executemany(
            f"INSERT INTO canyon.forecast_hours (forecast_id, {', '.join(hour_columns)})"
            f" VALUES (%s, {', '.join(['%s'] * len(hour_columns))})",
            [(forecast_id, *(h[c] for c in hour_columns)) for h in forecast.get("hours", [])],
        )


def refresh_forecast(
    pg: psycopg.Connection,
    title: str,
    *,
    max_age: timedelta = FORECAST_MAX_AGE,
    fetch: Callable[[float, float], dict[str, Any]] = fetch_forecast,
    now: datetime | None = None,
) -> bool:
    """Fetch a new forecast for the canyon when the stored one is older than ``max_age``.

    A failed request keeps the old forecast. Returns True if a new one was stored.
    """
    with pg.cursor() as cur:
        cur.execute(
            "SELECT c.id, ST_X(c.geom), ST_Y(c.geom), f.fetched_at"
            " FROM canyon.canyons c LEFT JOIN canyon.forecasts f ON f.canyon_id = c.id"
            " WHERE c.ropewiki_title = %s",
            (title,),
        )
        row = cur.fetchone()
    if row is None or row[1] is None:
        return False
    canyon_id, lon, lat, fetched = row
    stored = _parse_time(fetched)
    if stored and (now or datetime.now(UTC)) - stored < max_age:
        return False
    try:
        forecast = fetch(lat, lon)
    except (OSError, ValueError, KeyError) as err:
        log(f"forecast not updated for {title}: {err}")
        return False
    store_forecast(pg, canyon_id, forecast)
    return True


# -----------------------------------------------------------------------------
# sections, fingerprints and updates
# -----------------------------------------------------------------------------
# A canyon's data is split into sections that change independently on Ropewiki. Each has a
# fingerprint (a hash of its content, made here and only here); a client that stores the
# fingerprints of the sections it holds can ask which of them are out of date and fetch just
# those. Forecasts are not a section: the viewer refreshes them itself.

SECTIONS: dict[str, tuple[str, ...]] = {
    "canyon": ("canyons", "authors"),
    "geometry": ("kml_files", "routes", "waypoints", "rappels"),
    "beta": ("beta",),
    "links": ("links",),
    "conditions": ("conditions", "condition_rappel_notes", "condition_photos"),
    "ratings": ("ratings",),
}
SECTION_LABELS = {
    "canyon": "Canyon details",
    "geometry": "Tracks and rappels",
    "beta": "Description",
    "links": "Links",
    "conditions": "Condition reports",
    "ratings": "Ratings",
}
# Columns that change on every ingest without the content changing.
_VOLATILE = {"canyons": {"fetched_at"}, "kml_files": {"downloaded_at"}}
_LINK_COLUMNS = {"id", "canyon_id", "report_id", "kml_file_id", "forecast_id"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _section_content(pg: psycopg.Connection, canyon_id: int, section: str) -> dict[str, list]:
    """The content of a section as plain data, with row ids replaced by natural keys so the
    result does not change when rows are re-created."""
    out: dict[str, list] = {}
    for name in SECTIONS[section]:
        spec = _BY_NAME[name]
        cols = [
            c
            for c in _pg_columns(pg, name)
            if c not in _LINK_COLUMNS and c not in _VOLATILE.get(name, ())
        ]
        select = [
            sql.SQL("encode(ST_AsBinary(t.{}, 'NDR'), 'hex')").format(sql.Identifier(c))
            if c == spec.geom
            else sql.SQL("t.{}").format(sql.Identifier(c))
            for c in cols
        ]
        scope = spec.parents[0][0] if spec.parents else None
        if name == "canyons":
            query = sql.SQL("SELECT {} FROM {} t WHERE t.id = %s")
        elif scope == "report_id":  # a note or photo of a condition report: key it by the report
            select.append(sql.SQL("p.source_title"))
            query = sql.SQL(
                "SELECT {} FROM {} t JOIN canyon.conditions p ON p.id = t.report_id"
                " WHERE p.canyon_id = %s"
            )
        else:
            query = sql.SQL("SELECT {} FROM {} t WHERE t.canyon_id = %s")
        with pg.cursor() as cur:
            cur.execute(query.format(sql.SQL(", ").join(select), _ident(name)), (canyon_id,))
            out[name] = sorted((list(r) for r in cur.fetchall()), key=repr)
    return out


def section_digest(pg: psycopg.Connection, canyon_id: int, section: str) -> str:
    content = _section_content(pg, canyon_id, section)
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def refresh_stamps(pg: psycopg.Connection, canyon_id: int) -> dict[str, str]:
    """Recompute every section's fingerprint. A section's ``updated_at`` moves only when its
    content actually changed. Returns the new digests."""
    with pg.cursor() as cur:
        cur.execute(
            "SELECT section, digest FROM canyon.section_stamps WHERE canyon_id = %s", (canyon_id,)
        )
        old = dict(cur.fetchall())
    digests = {name: section_digest(pg, canyon_id, name) for name in SECTIONS}
    now = _now_iso()
    with pg.cursor() as cur:
        for name, digest in digests.items():
            if old.get(name) == digest:
                continue
            cur.execute(
                "INSERT INTO canyon.section_stamps (canyon_id, section, digest, updated_at)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT (canyon_id, section) DO UPDATE SET"
                " digest = EXCLUDED.digest, updated_at = EXCLUDED.updated_at",
                (canyon_id, name, digest, now),
            )
    return digests


def _layer_states(layers: dict[str, Any], now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Each tile layer the server's map holds: its build version and time, and whether it is
    out of date by the rules in tiles.py (a newer build version, or over a year old)."""
    from . import tiles as tile_rules

    out = {}
    for table, held in layers.items():
        why = tile_rules.layer_staleness(held.get("version"), held.get("built_at"), table, now)
        out[table] = {**held, "current_version": tile_rules.layer_version(table), "stale": why}
    return out


def manifest(pg: psycopg.Connection, title: str) -> dict[str, Any] | None:
    """What the server holds for a canyon: each section's fingerprint and when it last changed,
    plus when the data was last fetched from Ropewiki. None if the canyon is not stored."""
    with pg.cursor() as cur:
        cur.execute("SELECT id, fetched_at FROM canyon.canyons WHERE ropewiki_title = %s", (title,))
        row = cur.fetchone()
        if row is None:
            return None
        canyon_id, fetched_at = row
        cur.execute(
            "SELECT section, digest, updated_at FROM canyon.section_stamps WHERE canyon_id = %s",
            (canyon_id,),
        )
        stamps = {s: (d, u) for s, d, u in cur.fetchall()}
        cur.execute(
            "SELECT status, built_at, layers FROM canyon.tile_builds WHERE ropewiki_title = %s",
            (title,),
        )
        tiles = cur.fetchone()
    if set(stamps) != set(SECTIONS):  # data from before stamps existed
        refresh_stamps(pg, canyon_id)
        with pg.cursor() as cur:
            cur.execute(
                "SELECT section, digest, updated_at FROM canyon.section_stamps"
                " WHERE canyon_id = %s",
                (canyon_id,),
            )
            stamps = {s: (d, u) for s, d, u in cur.fetchall()}
    return {
        "title": title,
        "schema_version": SCHEMA_VERSION,
        "data_fetched_at": fetched_at,
        "sections": {
            name: {
                "label": SECTION_LABELS[name],
                "digest": stamps[name][0],
                "updated_at": stamps[name][1],
            }
            for name in SECTIONS
        },
        "map": {
            "status": tiles[0],
            "built_at": tiles[1],
            "layers": _layer_states(tiles[2] or {}),
        }
        if tiles
        else None,
    }


def outdated_sections(server: dict[str, Any], have: dict[str, str]) -> list[str]:
    """The sections whose fingerprint on the server differs from the client's (or the client
    has none)."""
    return [name for name, info in server["sections"].items() if have.get(name) != info["digest"]]


def section_delta(pg: psycopg.Connection, title: str, sections: list[str]) -> dict[str, Any]:
    """The rows of the named sections, to replace the client's copy of them.

    Tables are in parent-first order. Each carries its columns (``id`` first, so children can
    point at it), its foreign keys (column -> parent table), the geometry column (values are
    base64 of GeoPackage geometry blobs) and the rows. ``mode`` is ``update`` for the canyon
    row itself (changed in place) and ``replace`` for the rest. Fingerprints and times come
    along so the client can record what it now holds.
    """
    info = manifest(pg, title)
    if info is None:
        raise KeyError(title)
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        raise ValueError(f"unknown sections: {unknown}")
    with pg.cursor() as cur:
        cur.execute("SELECT id FROM canyon.canyons WHERE ropewiki_title = %s", (title,))
        canyon_id = cur.fetchone()[0]
    out: dict[str, Any] = {}
    for name in sections:
        tables = []
        scoped: dict[str, list[int]] = {}
        for table in SECTIONS[name]:
            spec = _BY_NAME[table]
            cols = _pg_columns(pg, table)
            select = [
                sql.SQL("ST_AsBinary({}, 'NDR')").format(sql.Identifier(c))
                if c == spec.geom
                else sql.Identifier(c)
                for c in cols
            ]
            if table == "canyons":
                where, params = sql.SQL("id = %s"), [canyon_id]
            else:
                scope_col, parent = spec.parents[0]
                where = sql.SQL("{} = ANY(%s)").format(sql.Identifier(scope_col))
                params = [[canyon_id] if parent == "canyons" else scoped.get(parent, [])]
            with pg.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY id").format(
                        sql.SQL(", ").join(select), _ident(table), where
                    ),
                    params,
                )
                rows = cur.fetchall()
            scoped[table] = [r[0] for r in rows]
            geom_at = cols.index(spec.geom) if spec.geom else None
            encoded = []
            for row in rows:
                row = list(row)
                if geom_at is not None and row[geom_at] is not None:
                    row[geom_at] = base64.b64encode(wkb_to_gpkg_blob(row[geom_at])).decode("ascii")
                encoded.append(row)
            tables.append(
                {
                    "name": table,
                    "mode": "update" if table == "canyons" else "replace",
                    "columns": cols,
                    "fks": {c: p for c, p in spec.parents},
                    "geometry": spec.geom,
                    "rows": encoded,
                }
            )
        out[name] = {
            "digest": info["sections"][name]["digest"],
            "updated_at": info["sections"][name]["updated_at"],
            "tables": tables,
        }
    return {"title": title, "schema_version": SCHEMA_VERSION, "sections": out}

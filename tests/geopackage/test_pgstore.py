from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from canyongis.geopackage import forecast as fc
from canyongis.geopackage import pgstore
from canyongis.geopackage.geometry import point_blob
from canyongis.geopackage.schema import create_geopackage, upgrade_geopackage

REPO = Path(__file__).resolve().parents[2]
LYTLE = REPO / "web" / "canyon-viewer" / "test" / "fixtures" / "lytle.gpkg"
TITLE = "Lytle Creek (Middle Fork)"
PAYLOAD = __import__("json").loads((REPO / "tests/fixtures/open-meteo.json").read_text())

# Columns that differ between the two sides by design (ids and links between rows).
_LINKS = {"id", "canyon_id", "report_id", "forecast_id", "kml_file_id"}


def _content(conn: sqlite3.Connection, table: str, only: list[str] | None = None) -> list[tuple]:
    """Rows of a GeoPackage table without ids and links, in a stable order. ``only`` limits the
    columns (the fixture is an older file with fewer columns than an export)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in _LINKS]
    if only is not None:
        cols = [c for c in cols if c in only]
    if not cols:  # the fixture predates this table (forecasts)
        return []
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    return sorted(rows, key=repr)


def _with_forecast(path: Path, fetched_at: str, source_file: Path = LYTLE) -> Path:
    """A copy of the Lytle GeoPackage carrying a forecast fetched at the given time."""
    import shutil

    shutil.copyfile(source_file, path)
    conn = sqlite3.connect(path)
    upgrade_geopackage(conn)  # the fixture is a version 2 file
    conn.execute("DELETE FROM forecasts")
    record = fc.parse_forecast(PAYLOAD, fetched_at)
    cid = conn.execute("SELECT id FROM canyons").fetchone()[0]
    fc.store_forecast(conn, cid, record)
    conn.commit()
    conn.close()
    return path


def test_postgis_and_geopackage_tables_have_the_same_columns(pg, tmp_path) -> None:
    gpkg = create_geopackage(tmp_path / "t.gpkg")
    for spec in pgstore.TABLES:
        sqlite_cols = {r[1] for r in gpkg.execute(f"PRAGMA table_info({spec.name})")}
        assert set(pgstore._pg_columns(pg, spec.name)) == sqlite_cols, spec.name


def test_geometry_blob_conversion() -> None:
    blob = point_blob(-117.5, 34.25)
    wkb = pgstore.gpkg_blob_to_wkb(blob)
    assert wkb is not None and len(wkb) == 21
    assert pgstore.wkb_to_gpkg_blob(wkb) == blob
    assert pgstore.gpkg_blob_to_wkb(None) is None
    with_envelope = b"GP\x00\x03" + blob[4:8] + bytes(32) + wkb  # envelope indicator 1
    assert pgstore.gpkg_blob_to_wkb(with_envelope) == wkb
    assert pgstore.gpkg_blob_to_wkb(b"GP\x00\x11" + blob[4:8]) is None  # empty geometry
    with pytest.raises(ValueError):
        pgstore.gpkg_blob_to_wkb(b"XX\x00\x01\x00\x00\x00\x00")


def test_round_trip_geopackage_to_postgis_and_back(pg, tmp_path) -> None:
    assert pgstore.load_geopackage(pg, LYTLE) == [TITLE]
    out = tmp_path / "out.gpkg"
    counts = pgstore.export_geopackage(pg, [TITLE], out)
    assert counts["canyons"] == 1 and counts["rappels"] == 5 and counts["conditions"] == 2

    src, back = sqlite3.connect(LYTLE), sqlite3.connect(out)
    for spec in pgstore.TABLES:
        have = [r[1] for r in src.execute(f"PRAGMA table_info({spec.name})")]
        assert _content(back, spec.name, have) == _content(src, spec.name), spec.name
    # links between rows survive the id remapping
    assert (
        back.execute(
            "SELECT count(*) FROM condition_rappel_notes n JOIN conditions c ON c.id = n.report_id"
        ).fetchone()
        == src.execute("SELECT count(*) FROM condition_rappel_notes").fetchone()
    )
    assert (
        back.execute(
            "SELECT count(*) FROM routes r JOIN kml_files k ON k.id = r.kml_file_id"
        ).fetchone()[0]
        == src.execute("SELECT count(*) FROM routes WHERE kml_file_id IS NOT NULL").fetchone()[0]
    )
    assert back.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone() == ("5",)
    assert back.execute("PRAGMA foreign_key_check").fetchall() == []


def test_export_on_top_of_a_tile_file_keeps_tiles_and_rewrites_rows(pg, tmp_path) -> None:
    pgstore.load_geopackage(pg, LYTLE)
    with pg.cursor() as cur:
        cur.execute("UPDATE canyon.canyons SET region = 'Changed in the database'")
    pg.commit()
    out = tmp_path / "with-tiles.gpkg"
    pgstore.export_geopackage(pg, [TITLE], out, base=LYTLE)
    src, back = sqlite3.connect(LYTLE), sqlite3.connect(out)
    tiles = [
        r[0] for r in src.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'tiles'")
    ]
    assert tiles
    for table in tiles:
        assert (
            back.execute(f"SELECT count(*) FROM {table}").fetchone()
            == src.execute(f"SELECT count(*) FROM {table}").fetchone()
        )
    assert back.execute("SELECT region FROM canyons").fetchone() == ("Changed in the database",)
    assert back.execute("SELECT count(*) FROM canyons").fetchone() == (1,)
    assert back.execute("SELECT count(*) FROM rappels").fetchone() == (5,)
    assert back.execute("SELECT min_x FROM gpkg_contents WHERE table_name = 'canyons'").fetchone()[
        0
    ]


def test_exporting_an_unknown_title_gives_an_empty_valid_file(pg, tmp_path) -> None:
    pgstore.load_geopackage(pg, LYTLE)
    out = tmp_path / "none.gpkg"
    assert pgstore.export_geopackage(pg, ["Nope"], out)["canyons"] == 0
    assert sqlite3.connect(out).execute("SELECT count(*) FROM canyons").fetchone() == (0,)


def test_reloading_keeps_the_canyon_id_and_replaces_children(pg) -> None:
    pgstore.load_geopackage(pg, LYTLE)
    first = pg.execute("SELECT id FROM canyon.canyons").fetchone()[0]
    pg.execute("DELETE FROM canyon.rappels WHERE sequence = 1")
    pg.execute(
        "INSERT INTO canyon.links (canyon_id, url) VALUES (%s, 'http://stale.example')", (first,)
    )
    pg.commit()
    pgstore.load_geopackage(pg, LYTLE)
    assert pg.execute("SELECT id FROM canyon.canyons").fetchone()[0] == first
    assert pg.execute("SELECT count(*) FROM canyon.rappels").fetchone()[0] == 5
    assert (
        pg.execute(
            "SELECT count(*) FROM canyon.links WHERE url = 'http://stale.example'"
        ).fetchone()[0]
        == 0
    )
    assert pg.execute("SELECT count(*) FROM canyon.canyons").fetchone()[0] == 1


def test_forecast_survives_a_reload_unless_the_file_has_a_newer_one(pg, tmp_path) -> None:
    now = datetime.now(UTC)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    pgstore.load_geopackage(pg, _with_forecast(tmp_path / "a.gpkg", iso(now - timedelta(hours=1))))
    stored = lambda: pg.execute("SELECT fetched_at FROM canyon.forecasts").fetchone()[0]  # noqa: E731
    assert stored() == iso(now - timedelta(hours=1))
    # a re-ingest from a file without a forecast keeps the stored one
    bare = _with_forecast(tmp_path / "bare.gpkg", "x")
    conn = sqlite3.connect(bare)
    conn.execute("DELETE FROM forecast_days")
    conn.execute("DELETE FROM forecasts")
    conn.commit()
    conn.close()
    pgstore.load_geopackage(pg, bare)
    assert stored() == iso(now - timedelta(hours=1))
    assert pg.execute("SELECT count(*) FROM canyon.forecast_days").fetchone()[0] == 3
    # an older one does not replace it, a newer one does (and days are replaced, not added)
    pgstore.load_geopackage(
        pg, _with_forecast(tmp_path / "old.gpkg", iso(now - timedelta(hours=9)))
    )
    assert stored() == iso(now - timedelta(hours=1))
    pgstore.load_geopackage(pg, _with_forecast(tmp_path / "new.gpkg", iso(now)))
    assert stored() == iso(now)
    assert pg.execute("SELECT count(*) FROM canyon.forecast_days").fetchone()[0] == 3


def test_refresh_forecast_respects_age_and_keeps_old_on_failure(pg) -> None:
    pgstore.load_geopackage(pg, LYTLE)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    calls: list[tuple[float, float]] = []

    def fetch(lat: float, lon: float) -> dict:
        calls.append((lat, lon))
        return fc.parse_forecast(PAYLOAD, "2026-11-09T12:00:00Z")

    assert pgstore.refresh_forecast(pg, TITLE, fetch=fetch, now=now) is True  # none stored yet
    assert calls and abs(calls[0][0] - 34.2533) < 1e-3 and abs(calls[0][1] + 117.57694) < 1e-3
    assert pgstore.refresh_forecast(pg, TITLE, fetch=fetch, now=now + timedelta(hours=1)) is False
    assert len(calls) == 1
    later = now + timedelta(hours=4)

    def offline(lat: float, lon: float) -> dict:
        raise OSError("network down")

    assert pgstore.refresh_forecast(pg, TITLE, fetch=offline, now=later) is False
    assert (
        pg.execute("SELECT fetched_at FROM canyon.forecasts").fetchone()[0]
        == "2026-11-09T12:00:00Z"
    )
    assert pgstore.refresh_forecast(pg, TITLE, fetch=fetch, now=later) is True
    assert pgstore.refresh_forecast(pg, "No such canyon", fetch=fetch, now=later) is False
    assert pg.execute("SELECT count(*) FROM canyon.forecast_days").fetchone()[0] == 3


def test_canyon_fetched_at(pg) -> None:
    assert pgstore.canyon_fetched_at(pg, TITLE) is None
    pgstore.load_geopackage(pg, LYTLE)
    got = pgstore.canyon_fetched_at(pg, TITLE)
    assert got is not None and got.tzinfo is not None


def test_hourly_forecast_values_are_stored_and_exported(pg, tmp_path) -> None:
    pgstore.load_geopackage(pg, LYTLE)
    fetch = lambda lat, lon: fc.parse_forecast(PAYLOAD, "2026-11-09T12:00:00Z")  # noqa: E731
    assert pgstore.refresh_forecast(pg, TITLE, fetch=fetch) is True
    assert pg.execute("SELECT count(*) FROM canyon.forecast_hours").fetchone()[0] == 72
    out = tmp_path / "hourly.gpkg"
    pgstore.export_geopackage(pg, [TITLE], out)
    back = sqlite3.connect(out)
    assert back.execute("SELECT count(*) FROM forecast_hours").fetchone() == (72,)
    assert back.execute(
        "SELECT weather_code FROM forecast_hours h JOIN forecasts f ON f.id = h.forecast_id"
        " WHERE h.time = '2026-11-11T14:00'"
    ).fetchone() == (95,)
    # a new forecast replaces the hours instead of adding to them
    later = "2026-11-10T12:00:00Z"
    pgstore.refresh_forecast(
        pg,
        TITLE,
        fetch=lambda lat, lon: fc.parse_forecast(PAYLOAD, later),
        now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )
    assert pg.execute("SELECT count(*) FROM canyon.forecast_hours").fetchone()[0] == 72

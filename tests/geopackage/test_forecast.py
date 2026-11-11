from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from canyongis.geopackage import forecast as fc
from canyongis.geopackage.geometry import point_blob
from canyongis.geopackage.schema import (
    _DDL_V3,
    ATTRIBUTE_TABLES,
    FEATURE_TABLES,
    create_geopackage,
    upgrade_geopackage,
)

REPO = Path(__file__).resolve().parents[2]
PAYLOAD = json.loads((REPO / "tests" / "fixtures" / "open-meteo.json").read_text())


def _canyon(conn, title="Lytle", lon=-117.5625, lat=34.25, with_geom=True):
    conn.execute(
        "INSERT INTO canyons (ropewiki_title, raw_template_json, source_url, fetched_at, geom)"
        " VALUES (?, '{}', 'u', 'now', ?)",
        (title, point_blob(lon, lat) if with_geom else None),
    )
    conn.commit()


def test_forecast_url_asks_for_the_daily_variables() -> None:
    url = fc.forecast_url(34.2533, -117.57694)
    assert url.startswith(fc.API_URL + "?latitude=34.2533&longitude=-117.5769")
    assert "daily=weather_code,temperature_2m_max" in url
    assert "timezone=auto" in url


def test_parse_forecast_maps_columns() -> None:
    f = fc.parse_forecast(PAYLOAD, "2026-11-09T12:00:00Z")
    assert (f["source"], f["fetched_at"], f["timezone"], f["elevation_m"]) == (
        "open-meteo",
        "2026-11-09T12:00:00Z",
        "America/Los_Angeles",
        1021.0,
    )
    assert len(f["days"]) == 3
    assert f["days"][2] == {
        "date": "2026-11-11",
        "weather_code": 95,
        "temp_max_c": 12.0,
        "temp_min_c": 5.9,
        "precip_mm": 21.7,
        "precip_prob_pct": 90,
        "wind_max_kmh": 31.5,
        "gust_max_kmh": 62.0,
        "sunrise": "2026-11-11T06:27",
        "sunset": "2026-11-11T16:49",
    }


def test_parse_forecast_rejects_empty_response() -> None:
    with pytest.raises(ValueError):
        fc.parse_forecast({"latitude": 1, "longitude": 2, "daily": {}})


def test_refresh_stores_and_replaces(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn)
    calls = []

    def fake(lat, lon):
        calls.append((lat, lon))
        return fc.parse_forecast(PAYLOAD, f"2026-11-09T12:00:0{len(calls)}Z")

    assert fc.refresh_forecasts(conn, fetch=fake) == ["Lytle"]
    assert fc.refresh_forecasts(conn, fetch=fake) == ["Lytle"]
    assert calls[0] == (34.25, -117.5625)
    assert conn.execute("SELECT count(*), max(fetched_at) FROM forecasts").fetchone() == (
        1,
        "2026-11-09T12:00:02Z",
    )
    assert conn.execute("SELECT count(*) FROM forecast_days").fetchone() == (3,)


def test_refresh_keeps_old_forecast_when_offline(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn)
    fc.refresh_forecasts(conn, fetch=lambda lat, lon: fc.parse_forecast(PAYLOAD, "old"))

    def offline(lat, lon):
        raise OSError("network down")

    assert fc.refresh_forecasts(conn, fetch=offline) == []
    assert conn.execute("SELECT fetched_at FROM forecasts").fetchone() == ("old",)


def test_refresh_skips_canyon_without_location_and_unlisted_titles(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn, "NoPoint", with_geom=False)
    _canyon(conn, "Other")
    fetch = lambda lat, lon: fc.parse_forecast(PAYLOAD)  # noqa: E731
    assert fc.refresh_forecasts(conn, ["NoPoint"], fetch=fetch) == []
    assert fc.refresh_forecasts(conn, ["Other"], fetch=fetch) == ["Other"]


def test_forecast_is_dropped_with_its_canyon(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn)
    fc.refresh_forecasts(conn, fetch=lambda lat, lon: fc.parse_forecast(PAYLOAD))
    conn.execute("DELETE FROM canyons")
    assert conn.execute("SELECT count(*) FROM forecast_days").fetchone() == (0,)


def test_new_tables_are_registered(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    contents = dict(conn.execute("SELECT table_name, data_type FROM gpkg_contents"))
    for name in ("runs", "run_events", "run_fixes"):
        assert contents[name] == "features"
        assert conn.execute(
            "SELECT geometry_type_name FROM gpkg_geometry_columns WHERE table_name = ?", (name,)
        ).fetchone()
    assert contents["forecasts"] == contents["forecast_days"] == "attributes"
    assert {t for t, *_ in FEATURE_TABLES} >= {"runs", "run_events", "run_fixes"}
    assert {t for t, _ in ATTRIBUTE_TABLES} >= {"forecasts", "forecast_days"}


def test_runs_survive_a_canyon_rebuild(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn)
    conn.execute(
        "INSERT INTO runs (run_uuid, canyon_title, started_at) VALUES ('r1', 'Lytle', 'x')"
    )
    conn.execute("DELETE FROM canyons WHERE ropewiki_title = 'Lytle'")
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_upgrade_adds_tables_to_a_version_2_file(tmp_path: Path) -> None:
    path = tmp_path / "old.gpkg"
    conn = create_geopackage(path)
    for name in ("run_fixes", "run_events", "runs", "forecast_days", "forecasts"):
        conn.execute(f"DROP TABLE {name}")
        conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (name,))
        conn.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", (name,))
    conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
    conn.commit()
    upgrade_geopackage(conn)
    upgrade_geopackage(conn)  # idempotent
    assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    assert conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == (
        "5",
    )
    assert conn.execute(
        "SELECT count(*) FROM gpkg_contents WHERE table_name IN ('runs', 'forecasts')"
    ).fetchone() == (2,)


def test_js_writer_carries_the_same_ddl() -> None:
    """web/canyon-viewer/gpkg-write.js mirrors _DDL_V3 and _DDL_V5 for files that predate them."""
    from canyongis.geopackage.schema import _COLUMNS_V4, _DDL_V5

    source = (REPO / "web" / "canyon-viewer" / "gpkg-write.js").read_text()

    def normalise(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip()

    for name, python_ddl in (("DDL_V3", _DDL_V3), ("DDL_V5", _DDL_V5)):
        match = re.search(rf"const {name} = `(.*?)`;", source, re.S)
        assert match, f"{name} template literal not found in gpkg-write.js"
        assert normalise(match.group(1)) == normalise(python_ddl), name
    js_columns = re.search(r"const COLUMNS_V4 = \[(.*?)\];", source, re.S).group(1)
    assert re.findall(r'\["(\w+)", "(\w+)", "(\w+)"\]', js_columns) == list(_COLUMNS_V4)


def test_hourly_values_are_parsed_in_the_columns_the_table_stores() -> None:
    f = fc.parse_forecast(PAYLOAD, "2026-11-09T12:00:00Z")
    assert len(f["hours"]) == 72
    assert f["hours"][0]["time"] == "2026-11-09T00:00"
    stormy = next(h for h in f["hours"] if h["time"] == "2026-11-11T14:00")
    assert stormy == {
        "time": "2026-11-11T14:00",
        "weather_code": 95,
        "temp_c": stormy["temp_c"],
        "precip_mm": 4.0,
        "precip_prob_pct": 80,
        "wind_kmh": stormy["wind_kmh"],
        "gust_kmh": stormy["gust_kmh"],
    }
    assert set(stormy) == {"time", *fc.HOURLY_VARIABLES.values()}
    assert "hourly=weather_code,temperature_2m" in fc.forecast_url(34.25, -117.5)
    no_hourly = {k: v for k, v in PAYLOAD.items() if k != "hourly"}
    assert fc.parse_forecast(no_hourly)["hours"] == []


def test_hourly_values_are_cached_and_replaced_with_the_forecast(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _canyon(conn)
    fetch = lambda lat, lon: fc.parse_forecast(PAYLOAD)  # noqa: E731
    fc.refresh_forecasts(conn, fetch=fetch)
    fc.refresh_forecasts(conn, fetch=fetch)
    assert conn.execute("SELECT count(*) FROM forecast_hours").fetchone() == (72,)
    row = conn.execute(
        "SELECT weather_code, precip_prob_pct FROM forecast_hours WHERE time = '2026-11-11T14:00'"
    ).fetchone()
    assert row == (95, 80)
    conn.execute("DELETE FROM canyons")  # foreign keys are on in the converter's connection
    assert conn.execute("SELECT count(*) FROM forecast_hours").fetchone() == (0,)

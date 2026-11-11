"""Weather forecasts for the canyon GeoPackage, from Open-Meteo (no API key).

One daily forecast per canyon is cached in ``forecasts`` / ``forecast_days`` with the time it
was fetched, so the canyon companion can show it offline and say how old it is. The companion
fetches a newer one itself when it has a network (web/canyon-viewer/forecast.js uses the same
request and the same column mapping; both are tested against tests/fixtures/open-meteo.json).

Open-Meteo's free endpoint is for non-commercial use: https://open-meteo.com/en/terms

Usage::

    canyongis-gpkg-forecast canyons.gpkg            # every canyon in the file
    canyongis-gpkg-forecast canyons.gpkg "Lytle Creek (Middle Fork)"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..io import log
from .geometry import blob_envelope
from .schema import open_geopackage, upgrade_geopackage

SOURCE = "open-meteo"
API_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7
# Open-Meteo variable -> forecast_days column. Units are the API defaults (degC, mm, km/h).
DAILY_VARIABLES = {
    "weather_code": "weather_code",
    "temperature_2m_max": "temp_max_c",
    "temperature_2m_min": "temp_min_c",
    "precipitation_sum": "precip_mm",
    "precipitation_probability_max": "precip_prob_pct",
    "wind_speed_10m_max": "wind_max_kmh",
    "wind_gusts_10m_max": "gust_max_kmh",
    "sunrise": "sunrise",
    "sunset": "sunset",
}
# Hourly values (the same units): Open-Meteo variable -> forecast_hours column.
HOURLY_VARIABLES = {
    "weather_code": "weather_code",
    "temperature_2m": "temp_c",
    "precipitation": "precip_mm",
    "precipitation_probability": "precip_prob_pct",
    "wind_speed_10m": "wind_kmh",
    "wind_gusts_10m": "gust_kmh",
}
_USER_AGENT = "RopeGeo/1.0"
_TIMEOUT_S = 30


def forecast_url(lat: float, lon: float) -> str:
    query = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "daily": ",".join(DAILY_VARIABLES),
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": "auto",
        "forecast_days": str(FORECAST_DAYS),
        "wind_speed_unit": "kmh",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
    }
    return f"{API_URL}?{urllib.parse.urlencode(query, safe=',')}"


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_forecast(payload: dict[str, Any], fetched_at: str | None = None) -> dict[str, Any]:
    """Turn an Open-Meteo response into a forecast record (header, day dicts and hour dicts)."""
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        raise ValueError("forecast response has no daily data")
    days = []
    for i, date in enumerate(dates):
        day: dict[str, Any] = {"date": date}
        for variable, column in DAILY_VARIABLES.items():
            values = daily.get(variable) or []
            day[column] = values[i] if i < len(values) else None
        days.append(day)
    hourly = payload.get("hourly") or {}
    hours = []
    for i, time in enumerate(hourly.get("time") or []):
        hour: dict[str, Any] = {"time": time}
        for variable, column in HOURLY_VARIABLES.items():
            values = hourly.get(variable) or []
            hour[column] = values[i] if i < len(values) else None
        hours.append(hour)
    return {
        "source": SOURCE,
        "fetched_at": fetched_at or _now_iso(),
        "latitude": payload["latitude"],
        "longitude": payload["longitude"],
        "elevation_m": payload.get("elevation"),
        "timezone": payload.get("timezone"),
        "days": days,
        "hours": hours,
    }


def fetch_forecast(lat: float, lon: float) -> dict[str, Any]:
    """Fetch and parse the forecast for a point. Raises OSError or ValueError on failure."""
    req = urllib.request.Request(forecast_url(lat, lon), headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("error"):
        raise ValueError(payload.get("reason") or "Open-Meteo returned an error")
    return parse_forecast(payload)


def store_forecast(conn: sqlite3.Connection, canyon_id: int, forecast: dict[str, Any]) -> None:
    """Replace the canyon's cached forecast."""
    conn.execute("DELETE FROM forecasts WHERE canyon_id = ?", (canyon_id,))
    cur = conn.execute(
        """INSERT INTO forecasts (canyon_id, source, fetched_at, latitude, longitude,
                                  elevation_m, timezone)
           VALUES (?,?,?,?,?,?,?)""",
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
    columns = ["date", *DAILY_VARIABLES.values()]
    conn.executemany(
        f"INSERT INTO forecast_days (forecast_id, {', '.join(columns)})"
        f" VALUES (?, {', '.join('?' for _ in columns)})",
        [(cur.lastrowid, *(day[c] for c in columns)) for day in forecast["days"]],
    )
    hour_columns = ["time", *HOURLY_VARIABLES.values()]
    conn.executemany(
        f"INSERT INTO forecast_hours (forecast_id, {', '.join(hour_columns)})"
        f" VALUES (?, {', '.join('?' for _ in hour_columns)})",
        [(cur.lastrowid, *(h[c] for c in hour_columns)) for h in forecast.get("hours", [])],
    )


def refresh_forecasts(
    conn: sqlite3.Connection,
    titles: list[str] | None = None,
    fetch: Callable[[float, float], dict[str, Any]] = fetch_forecast,
) -> list[str]:
    """Fetch a forecast for each canyon (all, or just ``titles``) and cache it.

    A canyon without a location, or whose request fails, keeps its existing forecast (if any)
    and is logged, so an offline build still works. Returns the titles that were updated.
    """
    rows = conn.execute("SELECT id, ropewiki_title, geom FROM canyons ORDER BY id").fetchall()
    updated: list[str] = []
    for canyon_id, title, geom in rows:
        if titles is not None and title not in titles:
            continue
        envelope = blob_envelope(geom) if geom else None
        if envelope is None:
            log(f"forecast skipped (no location): {title}")
            continue
        try:
            forecast = fetch(envelope[1], envelope[0])
        except (OSError, ValueError, KeyError) as err:
            log(f"forecast not updated for {title}: {err}")
            continue
        store_forecast(conn, canyon_id, forecast)
        conn.commit()
        updated.append(title)
        log(f"forecast stored for {title}: {len(forecast['days'])} days")
    return updated


def forecast_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="canyongis-gpkg-forecast",
        description="Fetch and cache a weather forecast for canyons in a GeoPackage.",
    )
    parser.add_argument("gpkg", help="existing canyon GeoPackage (older files are upgraded)")
    parser.add_argument("titles", nargs="*", help="canyon titles (default: every canyon)")
    args = parser.parse_args(argv)

    conn = open_geopackage(Path(args.gpkg))
    upgrade_geopackage(conn)
    updated = refresh_forecasts(conn, args.titles or None)
    conn.close()
    print(f"updated forecasts for {len(updated)} canyons in {args.gpkg}")


if __name__ == "__main__":
    forecast_cli()

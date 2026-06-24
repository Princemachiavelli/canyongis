"""Unit tests for the GeoLibre-plugin-facing server config (no DB needed)."""

from __future__ import annotations

from fastapi import Response

from canyongis.web.auth import SESSION_COOKIE_NAME, set_session_cookie
from canyongis.web.server import (
    _DEFAULT_CORS_ORIGINS,
    _cors_origins,
    _geolibre_project_export,
)


def test_cors_origins_default(monkeypatch) -> None:
    monkeypatch.delenv("CANYONGIS_CORS_ORIGINS", raising=False)
    origins = _cors_origins()
    assert origins == _DEFAULT_CORS_ORIGINS
    assert "http://localhost:5173" in origins
    assert "tauri://localhost" in origins


def test_cors_origins_env_extends_defaults(monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_CORS_ORIGINS", "https://maps.example.com, https://b.example.com")
    origins = _cors_origins()
    assert "https://maps.example.com" in origins
    assert "https://b.example.com" in origins
    assert "http://localhost:5173" in origins


def test_cors_origins_wildcard_replaces(monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_CORS_ORIGINS", "*")
    assert _cors_origins() == ["*"]


def _cookie_header(response: Response) -> str:
    value = response.headers.get("set-cookie")
    assert value is not None and SESSION_COOKIE_NAME in value
    return value.lower()


def test_session_cookie_defaults_to_lax(monkeypatch) -> None:
    monkeypatch.delenv("CANYONGIS_COOKIE_SAMESITE", raising=False)
    response = Response()
    set_session_cookie(response, "token")
    header = _cookie_header(response)
    assert "samesite=lax" in header
    assert "secure" not in header


def test_session_cookie_samesite_none_forces_secure(monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_COOKIE_SAMESITE", "none")
    response = Response()
    set_session_cookie(response, "token")
    header = _cookie_header(response)
    assert "samesite=none" in header
    assert "secure" in header


def test_session_cookie_invalid_value_falls_back_to_lax(monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_COOKIE_SAMESITE", "bogus")
    response = Response()
    set_session_cookie(response, "token")
    assert "samesite=lax" in _cookie_header(response)


def test_geolibre_project_export_wires_plugin() -> None:
    payload = _geolibre_project_export(
        project_id="abc123",
        name="Zion AOI",
        base_url="http://127.0.0.1:8765",
        bbox=(-113.1, 37.2, -112.9, 37.4),
    )
    assert payload["version"] == "0.1.0"
    assert payload["name"] == "Zion AOI"
    assert payload["layers"] == []
    plugins = payload["plugins"]
    assert plugins["manifestUrls"] == ["http://127.0.0.1:8765/plugins/canyongis/plugin.json"]
    assert plugins["activePluginIds"] == ["canyongis"]
    assert plugins["settings"]["canyongis"] == {
        "serverUrl": "http://127.0.0.1:8765",
        "projectId": "abc123",
        "autoLoad": True,
    }
    view = payload["mapView"]
    assert view["bbox"] == [-113.1, 37.2, -112.9, 37.4]
    assert view["center"] == [(-113.1 + -112.9) / 2, (37.2 + 37.4) / 2]


def test_geolibre_project_export_without_bounds() -> None:
    payload = _geolibre_project_export(
        project_id="abc123",
        name="Empty",
        base_url="http://127.0.0.1:8765",
        bbox=None,
    )
    assert "bbox" not in payload["mapView"]
    assert payload["mapView"]["center"]

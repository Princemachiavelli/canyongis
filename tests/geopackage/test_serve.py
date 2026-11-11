"""web/canyon-viewer/serve.py: static files, the /api routes and the /builds download route."""

from __future__ import annotations

import functools
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from canyongis.geopackage import viewer_api

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("viewer_serve", REPO / "web/canyon-viewer/serve.py")
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


class StubService:
    tiles_enabled = True
    max_age = viewer_api.MAX_AGE

    def search(self, text: str) -> list[dict]:
        return [{"title": f"found {text}"}]

    def request_build(self, title: str, *, want_tiles: bool = True):
        if title == "bad":
            raise ValueError("not a canyon")
        return type(
            "J", (), {"public": lambda self: {"id": "0" * 32, "title": title, "tiles": want_tiles}}
        )()

    def job(self, job_id: str):
        return None


@pytest.fixture
def server(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>viewer</h1>")
    (tmp_path / "builds" / "exports").mkdir(parents=True)
    (tmp_path / "builds" / "exports" / "good-12345678.gpkg").write_bytes(b"SQLite format 3\x00data")
    (tmp_path / "secret.gpkg").write_bytes(b"secret")
    (tmp_path / "builds" / "exports" / "notes.txt").write_text("not a gpkg")

    def start(service):
        handler = type(
            "H",
            (serve.ViewerHandler,),
            {"service": service, "build_dir": (tmp_path / "builds").resolve(), "gpkg_dir": None},
        )
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(handler, directory=str(tmp_path / "site"))
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    servers: list[ThreadingHTTPServer] = []
    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _request(url: str, data: bytes | None = None, ctype: str | None = None):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def test_static_files_and_manifest_type(server) -> None:
    base = server(StubService())
    assert _request(base + "/")[2] == b"<h1>viewer</h1>"
    assert serve.ViewerHandler.extensions_map[".webmanifest"] == "application/manifest+json"


def test_api_status_search_and_build(server) -> None:
    base = server(StubService())
    status, headers, body = _request(base + "/api/status")
    assert status == 200 and json.loads(body)["ok"] is True
    assert headers["Content-Type"] == "application/json" and headers["Cache-Control"] == "no-store"
    assert json.loads(_request(base + "/api/search?q=lytle")[2]) == {
        "results": [{"title": "found lytle"}]
    }
    post = json.dumps({"title": "Lytle", "tiles": False}).encode()
    status, _, body = _request(base + "/api/builds", post, "application/json")
    assert status == 202 and json.loads(body)["job"]["tiles"] is False
    assert _request(base + "/api/builds", post, "text/plain")[0] == 415
    assert _request(base + "/api/builds", b'{"title":"bad"}', "application/json")[0] == 400
    assert _request(base + "/api/builds/" + "0" * 32)[0] == 404
    assert _request(base + "/index.html", b"x", "application/json")[0] == 405


def test_api_is_a_json_404_when_disabled(server) -> None:
    base = server(None)
    status, headers, body = _request(base + "/api/status")
    assert status == 404 and "not enabled" in json.loads(body)["error"]
    assert headers["Content-Type"] == "application/json"
    assert _request(base + "/")[0] == 200  # the viewer itself still works


def test_builds_route_serves_only_plain_gpkg_files_from_exports(server) -> None:
    base = server(StubService())
    status, _, body = _request(base + "/builds/good-12345678.gpkg")
    assert status == 200 and body.startswith(b"SQLite format 3")
    assert _request(base + "/builds/notes.txt")[0] == 404
    assert _request(base + "/builds/missing.gpkg")[0] == 404
    assert _request(base + "/builds/..%2F..%2Fsecret.gpkg")[0] == 404
    assert _request(base + "/builds/%2e%2e/secret.gpkg")[0] == 404

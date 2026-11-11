"""Serve the canyon viewer over HTTPS (browsers only give a page GPS on HTTPS or localhost).

Normally started through the flake (`nix run .#canyon-viewer-serve`), which gets the
certificate from Tailscale. Static files need only the standard library:

    python serve.py --cert host.crt --key host.key --port 8443 --gpkg-dir .

Every ``*.gpkg`` directly inside ``--gpkg-dir`` is served at ``/gpkg/<name>``, so a phone can
open ``https://<host>:8443/?gpkg=gpkg/canyons.gpkg`` without copying the file over.

With ``--build-dir DIR`` the server also offers the "get a canyon" API the viewer uses to search
Ropewiki and download a GeoPackage built for any canyon (``/api/...``, finished files at
``/builds/<name>.gpkg``). That needs the canyongis package and a PostgreSQL with PostGIS
(CANYONGIS_PG_* settings, see ``canyongis.web.db``); ``DIR`` holds the exports and the per-canyon
map tile files and work folders.

``--http`` serves plain HTTP for local development (``localhost`` counts as a secure context).
"""

from __future__ import annotations

import argparse
import functools
import json
import ssl
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

GPKG_PREFIX = "/gpkg/"
BUILDS_PREFIX = "/builds/"
MAX_BODY_BYTES = 4096


class ViewerHandler(SimpleHTTPRequestHandler):
    gpkg_dir: Path | None = None
    build_dir: Path | None = None
    service: Any = None  # a canyongis.geopackage.viewer_api.BuildService when enabled
    extensions_map: ClassVar[dict[str, str]] = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".webmanifest": "application/manifest+json",
        ".wasm": "application/wasm",
    }

    def translate_path(self, path: str) -> str:
        route = unquote(urlsplit(path).path)
        for prefix, directory in (
            (GPKG_PREFIX, self.gpkg_dir),
            (BUILDS_PREFIX, self.build_dir / "exports" if self.build_dir else None),
        ):
            if route.startswith(prefix) and directory is not None:
                name = route[len(prefix) :]
                candidate = directory / name
                # Only plain .gpkg files in the directory itself, never a path out of it.
                if "/" not in name and name.endswith(".gpkg") and candidate.is_file():
                    return str(candidate)
                return str(directory / ".missing")
        return super().translate_path(path)

    # ---- the "get a canyon" API ---------------------------------------------------

    def _api(self, method: str) -> None:
        parsed = urlsplit(self.path)
        if self.service is None:
            status, payload = 404, {"error": "The canyon build API is not enabled on this server."}
        else:
            from canyongis.geopackage import viewer_api

            body = b""
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, MAX_BODY_BYTES + 1))
            status, payload = viewer_api.handle(
                self.service,
                method,
                parsed.path,
                parsed.query,
                body,
                self.headers.get("Content-Type", ""),
            )
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if urlsplit(self.path).path.startswith("/api/"):
            self._api("GET")
        else:
            super().do_GET()

    def do_POST(self) -> None:
        if urlsplit(self.path).path.startswith("/api/"):
            self._api("POST")
        else:
            self.send_error(405)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--http", action="store_true", help="plain HTTP (local development only)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="the viewer files (default: this directory)",
    )
    parser.add_argument(
        "--gpkg-dir", type=Path, default=None, help="serve its *.gpkg files at /gpkg/<name>"
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        help="enable the canyon search/build API; keeps exports and map tiles here",
    )
    parser.add_argument(
        "--no-tiles",
        action="store_true",
        help="build canyon data only, without the offline map tiles (no big downloads)",
    )
    args = parser.parse_args(argv)
    if not args.http and not (args.cert and args.key):
        parser.error("--cert and --key are required unless --http is given")
    host = args.host or ("127.0.0.1" if args.http else "0.0.0.0")

    service = None
    if args.build_dir:
        try:
            from canyongis.geopackage import viewer_api

            service = viewer_api.create_service(args.build_dir, tiles=not args.no_tiles)
            print(f"Canyon build API enabled (files in {args.build_dir})", flush=True)
        except Exception as err:  # no canyongis, or PostgreSQL not reachable: serve files only
            print(f"Canyon build API disabled: {err}", flush=True)

    handler = type(
        "Handler",
        (ViewerHandler,),
        {
            "gpkg_dir": args.gpkg_dir.resolve() if args.gpkg_dir else None,
            "build_dir": args.build_dir.resolve() if args.build_dir else None,
            "service": service,
        },
    )
    server = ThreadingHTTPServer(
        (host, args.port), functools.partial(handler, directory=str(args.directory))
    )
    scheme = "http"
    if not args.http:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"Serving {args.directory} on {scheme}://{host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

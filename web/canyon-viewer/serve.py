"""Serve the canyon viewer over HTTPS (browsers only give a page GPS on HTTPS or localhost).

Normally started through the flake (`nix run .#canyon-viewer-serve`), which gets the
certificate from Tailscale. Standard library only.

    python serve.py --cert host.crt --key host.key --port 8443 --gpkg-dir .

Every ``*.gpkg`` directly inside ``--gpkg-dir`` is served at ``/gpkg/<name>``, so a phone can
open ``https://<host>:8443/?gpkg=gpkg/canyons.gpkg`` without copying the file over.
"""

from __future__ import annotations

import argparse
import functools
import ssl
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

GPKG_PREFIX = "/gpkg/"


class ViewerHandler(SimpleHTTPRequestHandler):
    gpkg_dir: Path | None = None

    def translate_path(self, path: str) -> str:
        route = unquote(urlsplit(path).path)
        if route.startswith(GPKG_PREFIX) and self.gpkg_dir is not None:
            name = route[len(GPKG_PREFIX) :]
            candidate = self.gpkg_dir / name
            # Only plain .gpkg files in the directory itself, never a path out of it.
            if "/" not in name and name.endswith(".gpkg") and candidate.is_file():
                return str(candidate)
            return str(self.gpkg_dir / ".missing")
        return super().translate_path(path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
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
    args = parser.parse_args(argv)

    handler = type(
        "Handler",
        (ViewerHandler,),
        {
            "gpkg_dir": args.gpkg_dir.resolve() if args.gpkg_dir else None,
        },
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), functools.partial(handler, directory=str(args.directory))
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"Serving {args.directory} on https://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

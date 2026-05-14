"""Unified launcher that starts both the Web (FastAPI) and MCP servers concurrently.

All PostgreSQL and output-directory arguments are shared — specified once,
used by both servers. Run ``--help`` for the full list.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from getpass import getuser
from multiprocessing import Process


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="canyongis-server",
        description="Start the canyongis Web (FastAPI) and MCP servers concurrently.",
    )

    # Shared PostgreSQL
    pg = p.add_argument_group("PostgreSQL connection (shared)")
    pg.add_argument("--pg-host", default=os.environ.get("CANYONGIS_PG_HOST", "localhost"))
    pg.add_argument("--pg-port", type=int, default=int(os.environ.get("CANYONGIS_PG_PORT", "5433")))
    pg.add_argument("--pg-db", default=os.environ.get("CANYONGIS_PG_DB", "postgres"))
    pg.add_argument(
        "--pg-user",
        default=os.environ.get("CANYONGIS_PG_USER", os.environ.get("PGUSER", getuser())),
    )
    pg.add_argument(
        "--pg-password",
        default=os.environ.get("CANYONGIS_PG_PASSWORD", os.environ.get("PGPASSWORD")),
    )
    pg.add_argument("--pg-dsn", default=os.environ.get("CANYONGIS_PG_DSN"))

    # Shared output root
    p.add_argument("--output-root", default="runs/web", help="Job/output root (default: runs/web).")

    # Shared frontend dist
    p.add_argument(
        "--frontend-dist",
        default=os.environ.get("CANYONGIS_FRONTEND_DIST", "web/frontend/dist"),
        help="Built frontend directory to serve if present.",
    )

    # Web server
    web = p.add_argument_group("Web server (FastAPI)")
    web.add_argument(
        "--web-host",
        default="127.0.0.1",
        dest="web_host",
        help="Web server bind host (default: 127.0.0.1).",
    )
    web.add_argument(
        "--web-port",
        type=int,
        default=8765,
        dest="web_port",
        help="Web server bind port (default: 8765).",
    )

    # MCP server
    mcp = p.add_argument_group("MCP server (FastMCP)")
    mcp.add_argument(
        "--mcp-transport",
        default="stdio",
        choices=["stdio", "sse"],
        dest="mcp_transport",
        help="MCP transport (default: stdio).",
    )
    mcp.add_argument(
        "--mcp-host",
        default="127.0.0.1",
        dest="mcp_host",
        help="MCP bind host for SSE transport (default: 127.0.0.1).",
    )
    mcp.add_argument(
        "--mcp-port",
        type=int,
        default=9876,
        dest="mcp_port",
        help="MCP bind port for SSE transport (default: 9876).",
    )

    p.add_argument("--no-web", action="store_true", help="Skip starting the web server.")
    p.add_argument("--no-mcp", action="store_true", help="Skip starting the MCP server.")
    return p


def _start_web(args: argparse.Namespace) -> None:
    web_argv = [
        "--host",
        args.web_host,
        "--port",
        str(args.web_port),
        "--output-root",
        args.output_root,
        "--frontend-dist",
        args.frontend_dist,
        "--pg-host",
        args.pg_host,
        "--pg-port",
        str(args.pg_port),
        "--pg-db",
        args.pg_db,
        "--pg-user",
        args.pg_user,
        "--pg-password",
        args.pg_password or "",
    ]
    if args.pg_dsn:
        web_argv.extend(["--pg-dsn", args.pg_dsn])

    from .server import cli as web_cli

    web_cli(web_argv)


def _start_mcp(args: argparse.Namespace) -> None:
    mcp_argv = [
        "--transport",
        args.mcp_transport,
        "--host",
        args.mcp_host,
        "--port",
        str(args.mcp_port),
        "--output-root",
        args.output_root,
        "--pg-host",
        args.pg_host,
        "--pg-port",
        str(args.pg_port),
        "--pg-db",
        args.pg_db,
        "--pg-user",
        args.pg_user,
        "--pg-password",
        args.pg_password or "",
    ]
    if args.pg_dsn:
        mcp_argv.extend(["--pg-dsn", args.pg_dsn])

    from .mcp_server import cli as mcp_cli

    mcp_cli(mcp_argv)


def cli(argv: list[str] | None = None) -> None:
    p = _build_arg_parser()
    args = p.parse_args(argv)

    if args.no_web and args.no_mcp:
        p.error("both --no-web and --no-mcp specified; nothing to start")

    procs: list[Process] = []

    if not args.no_web:
        w = Process(target=_start_web, args=(args,), name="canyongis-web")
        w.daemon = True
        procs.append(w)

    if not args.no_mcp:
        m = Process(target=_start_mcp, args=(args,), name="canyongis-mcp")
        m.daemon = True
        procs.append(m)

    def _shutdown(_signum: int, _frame) -> None:
        for p in procs:
            if p.is_alive():
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for p in procs:
        p.start()

    print(
        f"canyongis-server started\n"
        f"  Web:  http://{args.web_host}:{args.web_port}\n"
        f"  MCP:  {args.mcp_transport}://{args.mcp_host}:{args.mcp_port}"
        if not args.no_web
        else "",
        file=sys.stderr,
    )

    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        _shutdown(None, None)


if __name__ == "__main__":
    cli()

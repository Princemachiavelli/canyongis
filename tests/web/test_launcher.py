from __future__ import annotations

import sys
from argparse import Namespace

import pytest

from canyongis.web import launcher


def test_start_web_builds_shared_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    fake_server = Namespace(cli=lambda argv: calls.append(argv))
    monkeypatch.setitem(sys.modules, "canyongis.web.server", fake_server)
    args = Namespace(
        web_host="0.0.0.0",
        web_port=8000,
        output_root="out",
        frontend_dist="dist",
        pg_host="/tmp/pg",
        pg_port=5432,
        pg_db="db",
        pg_user="user",
        pg_password=None,
        pg_dsn="postgresql://custom/db",
    )

    launcher._start_web(args)

    assert calls == [
        [
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--output-root",
            "out",
            "--frontend-dist",
            "dist",
            "--pg-host",
            "/tmp/pg",
            "--pg-port",
            "5432",
            "--pg-db",
            "db",
            "--pg-user",
            "user",
            "--pg-password",
            "",
            "--pg-dsn",
            "postgresql://custom/db",
        ]
    ]


def test_start_mcp_builds_shared_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    fake_mcp = Namespace(cli=lambda argv: calls.append(argv))
    monkeypatch.setitem(sys.modules, "canyongis.web.mcp_server", fake_mcp)
    args = Namespace(
        mcp_transport="sse",
        mcp_host="127.0.0.1",
        mcp_port=9000,
        output_root="out",
        pg_host="localhost",
        pg_port=5433,
        pg_db="postgres",
        pg_user="user",
        pg_password="secret",
        pg_dsn=None,
    )

    launcher._start_mcp(args)

    assert calls == [
        [
            "--transport",
            "sse",
            "--host",
            "127.0.0.1",
            "--port",
            "9000",
            "--output-root",
            "out",
            "--pg-host",
            "localhost",
            "--pg-port",
            "5433",
            "--pg-db",
            "postgres",
            "--pg-user",
            "user",
            "--pg-password",
            "secret",
        ]
    ]


def test_cli_rejects_starting_nothing() -> None:
    with pytest.raises(SystemExit) as excinfo:
        launcher.cli(["--no-web", "--no-mcp"])

    assert excinfo.value.code == 2


def test_cli_starts_selected_processes(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    started = []
    joined = []
    signal_calls = []

    class FakeProcess:
        def __init__(self, *, target, args, name):
            self.target = target
            self.args = args
            self.name = name

        def start(self):
            started.append(self.name)

        def join(self):
            joined.append(self.name)

        def is_alive(self):
            return False

        def terminate(self):  # pragma: no cover - guarded by is_alive in this test
            raise AssertionError("should not terminate")

    monkeypatch.setattr(launcher, "Process", FakeProcess)
    monkeypatch.setattr(launcher.signal, "signal", lambda sig, fn: signal_calls.append((sig, fn)))

    launcher.cli(["--no-mcp", "--web-port", "8766"])

    assert started == ["canyongis-web"]
    assert joined == ["canyongis-web"]
    assert len(signal_calls) == 2
    assert "http://127.0.0.1:8766" in capsys.readouterr().err

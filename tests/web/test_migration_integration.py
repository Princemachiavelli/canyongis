"""End-to-end migration + auth + asset backfill tests against a live postgres.

Skipped when no local postgres is reachable. The dev shell starts one on
port 5433 with trust auth, so these run in ``nix develop``.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from canyongis.web.auth import LOCAL_USERNAME, build_auth_manager
from canyongis.web.db import PostgresConfig, apply_migrations
from canyongis.web.server import create_app

pytest.importorskip("httpx")  # fastapi.testclient requires httpx
from fastapi.testclient import TestClient  # noqa: E402


def _legacy_schema(pg: PostgresConfig) -> None:
    """Reproduce the pre-Alembic schema as if upgrading an existing install."""
    with psycopg.connect(**pg.connect_kwargs()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE canyongis_jobs (
                    id text PRIMARY KEY,
                    output_root text NOT NULL DEFAULT '',
                    kind text NOT NULL,
                    status text NOT NULL,
                    created_at text NOT NULL,
                    updated_at text NOT NULL,
                    params jsonb NOT NULL DEFAULT '{}'::jsonb,
                    result jsonb,
                    error text,
                    progress jsonb NOT NULL DEFAULT '[]'::jsonb
                )
                """
            )


def _insert_legacy_job(
    pg: PostgresConfig,
    job_id: str,
    *,
    output_root: str,
    result: dict | None,
) -> None:
    now = _dt.datetime.now(_dt.UTC).isoformat()
    with psycopg.connect(**pg.connect_kwargs()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO canyongis_jobs
                    (id, output_root, kind, status, created_at, updated_at,
                     params, result, error, progress)
                VALUES (%s, %s, 'build-dem', 'succeeded', %s, %s, %s, %s, NULL, %s)
                """,
                (
                    job_id,
                    output_root,
                    now,
                    now,
                    Jsonb({}),
                    Jsonb(result) if result is not None else None,
                    Jsonb([]),
                ),
            )


def _count(pg: PostgresConfig, sql: str, *params) -> int:
    with psycopg.connect(**pg.connect_kwargs()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_migration_stamps_baseline_when_legacy_schema_present(
    live_postgres: PostgresConfig, tmp_path: Path
) -> None:
    _legacy_schema(live_postgres)
    out = tmp_path / "out"
    out.mkdir()
    dem = out / "areas" / "aoi" / "dem.tif"
    dem.parent.mkdir(parents=True)
    dem.write_bytes(b"fake-tif")
    _insert_legacy_job(
        live_postgres,
        "job-legacy",
        output_root=str(out.resolve()),
        result={"dem_path": str(dem), "bounds_wgs84": [1, 2, 3, 4]},
    )

    apply_migrations(live_postgres)

    # Local user + Personal project created.
    assert _count(live_postgres, "SELECT count(*) FROM canyongis_users") == 1
    assert _count(live_postgres, "SELECT count(*) FROM canyongis_projects") == 1
    # Legacy job got attributed to the local user.
    assert (
        _count(
            live_postgres,
            "SELECT count(*) FROM canyongis_jobs WHERE project_id IS NOT NULL",
        )
        == 1
    )
    # Asset row backfilled from result.dem_path.
    assert _count(live_postgres, "SELECT count(*) FROM canyongis_assets") == 1
    with psycopg.connect(**live_postgres.connect_kwargs()) as conn, conn.cursor() as cur:
        cur.execute("SELECT kind, path FROM canyongis_assets")
        kind, path = cur.fetchone()
        assert kind == "tif"
        assert Path(path) == dem.resolve()


def test_migration_creates_local_user_on_empty_db(
    live_postgres: PostgresConfig,
) -> None:
    apply_migrations(live_postgres)
    assert (
        _count(live_postgres, "SELECT count(*) FROM canyongis_users WHERE username = %s", "local")
        == 1
    )
    assert _count(live_postgres, "SELECT count(*) FROM canyongis_projects") == 1


def test_login_logout_flow(live_postgres: PostgresConfig, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CANYONGIS_SINGLE_USER", raising=False)
    app = create_app(output_root=tmp_path, postgres=live_postgres)
    client = TestClient(app)

    # Anonymous → 401 on protected route.
    assert client.get("/api/jobs").status_code == 401

    # /auth/me reports unauthenticated when not single-user.
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is False
    assert me["single_user"] is False

    # Login as the seeded local user.
    res = client.post("/api/auth/login", json={"username": LOCAL_USERNAME, "password": "local"})
    assert res.status_code == 200, res.text
    assert "canyongis_session" in client.cookies

    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["user"]["username"] == LOCAL_USERNAME

    assert client.get("/api/jobs").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/jobs").status_code == 401


def test_single_user_mode_skips_login(
    live_postgres: PostgresConfig, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path, postgres=live_postgres)
    client = TestClient(app)
    # No cookie, no login — yet authenticated.
    assert client.get("/api/jobs").status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["single_user"] is True
    assert me["user"]["username"] == LOCAL_USERNAME


def test_login_rejects_wrong_password(
    live_postgres: PostgresConfig, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CANYONGIS_SINGLE_USER", raising=False)
    app = create_app(output_root=tmp_path, postgres=live_postgres)
    client = TestClient(app)
    res = client.post("/api/auth/login", json={"username": LOCAL_USERNAME, "password": "wrong"})
    assert res.status_code == 401


def test_auth_manager_create_and_authenticate(
    live_postgres: PostgresConfig, tmp_path: Path
) -> None:
    apply_migrations(live_postgres)
    auth = build_auth_manager(live_postgres, tmp_path)
    user = auth.create_user("alice", "wonderland")
    assert auth.authenticate("alice", "wonderland") == user
    assert auth.authenticate("alice", "elsewhere") is None
    assert auth.get_user_by_id(user.id) == user

"""Shared fixtures for canyongis.web tests.

The ``live_postgres`` fixture creates a dedicated test database on the Nix
devshell PostgreSQL instance at ``localhost:5433``. It intentionally avoids
``PGHOST``/socket paths so SQLAlchemy and psycopg use the same TCP target.
Tests that don't need a DB ignore it. Tests requesting it are skipped when no
postgres is reachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from canyongis.web.db import PostgresConfig


def _admin_config() -> PostgresConfig:
    """Return the admin connection used to create per-test databases.

    Keep this on TCP localhost rather than honoring ``PGHOST``. The devshell may
    set ``PGHOST`` to a Unix socket directory, which works for psycopg but caused
    SQLAlchemy URL construction to treat the socket path as part of the dbname.
    Each test still gets a random ``canyongis_test_*`` database, so the existing
    dev database is not modified.
    """

    return PostgresConfig(
        host=os.environ.get("CANYONGIS_TEST_PG_HOST", "localhost"),
        port=int(os.environ.get("CANYONGIS_TEST_PG_PORT", "5433")),
        dbname=os.environ.get("CANYONGIS_TEST_PG_ADMIN_DB", "postgres"),
        user=os.environ.get("CANYONGIS_TEST_PG_USER", os.environ.get("PGUSER")),
        password=os.environ.get("CANYONGIS_TEST_PG_PASSWORD", os.environ.get("PGPASSWORD")),
    )


@pytest.fixture
def live_postgres() -> Iterator[PostgresConfig]:
    import psycopg
    from psycopg import sql

    admin = _admin_config()
    try:
        conn = psycopg.connect(**admin.connect_kwargs())
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable at {admin.host}:{admin.port}: {exc}")
    conn.autocommit = True
    db_name = f"canyongis_test_{uuid.uuid4().hex[:10]}"
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    conn.close()

    cfg = PostgresConfig(
        host=admin.host,
        port=admin.port,
        dbname=db_name,
        user=admin.user,
        password=admin.password,
    )
    try:
        yield cfg
    finally:
        admin_conn = psycopg.connect(**admin.connect_kwargs())
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (db_name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
        admin_conn.close()

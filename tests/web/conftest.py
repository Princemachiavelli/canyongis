"""Shared fixtures for canyongis.web tests.

The ``live_postgres`` fixture creates a dedicated test database on whatever
PostgreSQL instance the local environment is using (``PGHOST``/``CANYONGIS_PG_*``).
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
    return PostgresConfig(
        host=os.environ.get("PGHOST", os.environ.get("CANYONGIS_PG_HOST", "localhost")),
        port=int(os.environ.get("PGPORT", os.environ.get("CANYONGIS_PG_PORT", "5433"))),
        dbname=os.environ.get("PGDATABASE", os.environ.get("CANYONGIS_PG_DB", "postgres")),
        user=os.environ.get("PGUSER", os.environ.get("CANYONGIS_PG_USER")),
        password=os.environ.get("PGPASSWORD", os.environ.get("CANYONGIS_PG_PASSWORD")),
    )


@pytest.fixture
def live_postgres() -> Iterator[PostgresConfig]:
    import psycopg

    admin = _admin_config()
    try:
        conn = psycopg.connect(**admin.connect_kwargs())
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable at {admin.host}:{admin.port}: {exc}")
    conn.autocommit = True
    db_name = f"canyongis_test_{uuid.uuid4().hex[:10]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{db_name}"')
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
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        admin_conn.close()

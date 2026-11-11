"""PostGIS fixtures for the canyon store tests (skipped when no Postgres is reachable)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from canyongis.web.db import PostgresConfig, apply_migrations
from tests.web.conftest import _admin_config


@pytest.fixture(scope="module")
def canyon_db() -> Iterator[PostgresConfig]:
    """A database with all migrations applied, shared by the tests of one module."""
    admin = _admin_config()
    try:
        conn = psycopg.connect(**admin.connect_kwargs())
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable at {admin.host}:{admin.port}: {exc}")
    conn.autocommit = True
    name = f"canyongis_test_{uuid.uuid4().hex[:10]}"
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn.close()
    cfg = PostgresConfig(
        host=admin.host, port=admin.port, dbname=name, user=admin.user, password=admin.password
    )
    try:
        apply_migrations(cfg)
        yield cfg
    finally:
        with psycopg.connect(**admin.connect_kwargs(), autocommit=True) as cleanup:
            cleanup.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            cleanup.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def pg(canyon_db: PostgresConfig) -> Iterator[psycopg.Connection]:
    """A connection to the shared database with the canyon tables emptied first."""
    conn = psycopg.connect(**canyon_db.connect_kwargs())
    conn.execute("TRUNCATE canyon.canyons, canyon.tile_builds RESTART IDENTITY CASCADE")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()

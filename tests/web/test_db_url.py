"""Unit tests for PostgresConfig.sqlalchemy_url() — pure, no database."""

from __future__ import annotations

from canyongis.web.db import PostgresConfig


def test_sqlalchemy_url_basic() -> None:
    pg = PostgresConfig(host="db.internal", port=5432, dbname="canyon", user="alice")
    assert pg.sqlalchemy_url() == "postgresql+psycopg://alice@db.internal:5432/canyon"


def test_sqlalchemy_url_no_user() -> None:
    pg = PostgresConfig(host="localhost", port=5433, dbname="postgres")
    assert pg.sqlalchemy_url() == "postgresql+psycopg://localhost:5433/postgres"


def test_sqlalchemy_url_escapes_user_password() -> None:
    pg = PostgresConfig(host="db", port=5432, dbname="d", user="al ice", password="pa@ss")
    url = pg.sqlalchemy_url()
    assert "al%20ice" in url
    assert "pa%40ss" in url


def test_sqlalchemy_url_from_dsn_prefix_swap() -> None:
    pg = PostgresConfig(dsn="postgresql://alice:s@host/db")
    assert pg.sqlalchemy_url() == "postgresql+psycopg://alice:s@host/db"

    pg2 = PostgresConfig(dsn="postgres://alice@host/db")
    assert pg2.sqlalchemy_url() == "postgresql+psycopg://alice@host/db"

    pg3 = PostgresConfig(dsn="postgresql+psycopg://alice@host/db")
    assert pg3.sqlalchemy_url() == "postgresql+psycopg://alice@host/db"

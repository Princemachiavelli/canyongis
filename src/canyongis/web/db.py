"""PostgreSQL connection config shared by the FastAPI server and Alembic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg


@dataclass
class PostgresConfig:
    host: str = "localhost"
    port: int = 5433
    dbname: str = "postgres"
    user: str | None = None
    password: str | None = None
    dsn: str | None = None

    def connect_kwargs(self) -> dict[str, Any]:
        if self.dsn:
            return {"conninfo": self.dsn}
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
        }
        if self.user:
            kwargs["user"] = self.user
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    def sqlalchemy_url(self) -> str:
        """Return a SQLAlchemy/Alembic-compatible URL using the psycopg v3 driver."""
        if self.dsn:
            if self.dsn.startswith("postgresql+"):
                return self.dsn
            if self.dsn.startswith("postgresql://"):
                return self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
            if self.dsn.startswith("postgres://"):
                return self.dsn.replace("postgres://", "postgresql+psycopg://", 1)
            return f"postgresql+psycopg://?{self.dsn}"
        userinfo = ""
        if self.user:
            userinfo = quote(self.user, safe="")
            if self.password:
                userinfo = f"{userinfo}:{quote(self.password, safe='')}"
            userinfo = f"{userinfo}@"
        return f"postgresql+psycopg://{userinfo}{self.host}:{self.port}/{self.dbname}"


def postgres_config_from_env() -> PostgresConfig:
    """Build a PostgresConfig from CANYONGIS_PG_* (or PGUSER/PGPASSWORD) env vars."""
    return PostgresConfig(
        host=os.environ.get("CANYONGIS_PG_HOST", "localhost"),
        port=int(os.environ.get("CANYONGIS_PG_PORT", "5433")),
        dbname=os.environ.get("CANYONGIS_PG_DB", "postgres"),
        user=os.environ.get("CANYONGIS_PG_USER", os.environ.get("PGUSER", getuser())),
        password=os.environ.get("CANYONGIS_PG_PASSWORD", os.environ.get("PGPASSWORD")),
        dsn=os.environ.get("CANYONGIS_PG_DSN"),
    )


def _alembic_config(pg: PostgresConfig):
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not ini.exists():
        # When the package is installed (no repo root), fall back to the
        # in-package alembic directory and synthesize an inline config.
        script_location = str(Path(__file__).resolve().parent / "alembic")
        cfg = Config()
        cfg.set_main_option("script_location", script_location)
    else:
        cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", pg.sqlalchemy_url())
    cfg.attributes["postgres_config"] = pg
    return cfg


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table_name,),
        )
        return cur.fetchone() is not None


BASELINE_REVISION = "0001_initial_schema"


def apply_migrations(pg: PostgresConfig) -> None:
    """Bring the database up to ``alembic head``.

    If the database already has ``canyongis_jobs`` from the legacy
    ``_ensure_schema`` path but no ``alembic_version`` table, stamp it at the
    baseline first so the baseline migration is treated as already applied.
    """
    from alembic import command

    with psycopg.connect(**pg.connect_kwargs()) as conn:
        conn.autocommit = True
        has_alembic = _table_exists(conn, "alembic_version")
        has_jobs = _table_exists(conn, "canyongis_jobs")

    cfg = _alembic_config(pg)
    if not has_alembic and has_jobs:
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")

"""Alembic environment for canyongis.

The URL comes from PostgresConfig — either injected via
``config.attributes['postgres_config']`` (when run from server.py) or built
from CANYONGIS_PG_* env vars when invoked from the CLI.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from canyongis.web.db import PostgresConfig, postgres_config_from_env

config = context.config

pg: PostgresConfig | None = config.attributes.get("postgres_config")
if pg is None:
    pg = postgres_config_from_env()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(pg.sqlalchemy_engine_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

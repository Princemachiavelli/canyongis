"""Initial canyongis_jobs schema (baseline).

This reproduces the schema previously created by JobStore._ensure_schema().
It is idempotent (``IF NOT EXISTS``) so that databases pre-dating Alembic
can be stamped at this revision and re-run without error.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS canyongis_jobs (
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS canyongis_jobs_created_at_idx "
        "ON canyongis_jobs (created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS canyongis_jobs_output_root_idx "
        "ON canyongis_jobs (output_root)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS canyongis_jobs_kind_idx ON canyongis_jobs (kind)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS canyongis_jobs_status_idx ON canyongis_jobs (status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS canyongis_jobs")

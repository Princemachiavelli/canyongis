"""Add users, projects, project_memberships, assets + multi-user columns on jobs.

Revision ID: 0002_multi_user_tables
Revises: 0001_initial_schema
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision = "0002_multi_user_tables"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE canyongis_users (
            id text PRIMARY KEY,
            username text NOT NULL UNIQUE,
            password_hash text NOT NULL,
            created_at text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE canyongis_projects (
            id text PRIMARY KEY,
            name text NOT NULL,
            created_by_user_id text NOT NULL REFERENCES canyongis_users(id),
            created_at text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text
        )
        """
    )
    op.execute(
        "CREATE INDEX canyongis_projects_created_by_user_id_idx "
        "ON canyongis_projects (created_by_user_id)"
    )
    op.execute(
        """
        CREATE TABLE canyongis_project_memberships (
            project_id text NOT NULL REFERENCES canyongis_projects(id) ON DELETE CASCADE,
            user_id text NOT NULL REFERENCES canyongis_users(id) ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
            created_at text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text,
            PRIMARY KEY (project_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX canyongis_project_memberships_user_id_idx "
        "ON canyongis_project_memberships (user_id)"
    )

    op.execute(
        """
        ALTER TABLE canyongis_jobs
            ADD COLUMN project_id text REFERENCES canyongis_projects(id),
            ADD COLUMN created_by_user_id text REFERENCES canyongis_users(id),
            ADD COLUMN started_at text,
            ADD COLUMN finished_at text,
            ADD COLUMN canceled_at text,
            ADD COLUMN canceled_by_user_id text REFERENCES canyongis_users(id)
        """
    )
    op.execute("CREATE INDEX canyongis_jobs_project_id_idx ON canyongis_jobs (project_id)")
    op.execute(
        "CREATE INDEX canyongis_jobs_created_by_user_id_idx ON canyongis_jobs (created_by_user_id)"
    )

    op.execute(
        """
        CREATE TABLE canyongis_assets (
            id text PRIMARY KEY,
            project_id text NOT NULL REFERENCES canyongis_projects(id) ON DELETE CASCADE,
            job_id text REFERENCES canyongis_jobs(id) ON DELETE CASCADE,
            created_by_user_id text REFERENCES canyongis_users(id),
            kind text NOT NULL,
            path text NOT NULL,
            url text,
            bounds_wgs84 jsonb,
            crs text,
            format text,
            media_type text,
            size_bytes bigint,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text,
            deleted_at text
        )
        """
    )
    op.execute("CREATE INDEX canyongis_assets_project_id_idx ON canyongis_assets (project_id)")
    op.execute("CREATE INDEX canyongis_assets_job_id_idx ON canyongis_assets (job_id)")
    op.execute("CREATE INDEX canyongis_assets_kind_idx ON canyongis_assets (kind)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS canyongis_assets")
    op.execute(
        """
        ALTER TABLE canyongis_jobs
            DROP COLUMN IF EXISTS canceled_by_user_id,
            DROP COLUMN IF EXISTS canceled_at,
            DROP COLUMN IF EXISTS finished_at,
            DROP COLUMN IF EXISTS started_at,
            DROP COLUMN IF EXISTS created_by_user_id,
            DROP COLUMN IF EXISTS project_id
        """
    )
    op.execute("DROP TABLE IF EXISTS canyongis_project_memberships")
    op.execute("DROP TABLE IF EXISTS canyongis_projects")
    op.execute("DROP TABLE IF EXISTS canyongis_users")

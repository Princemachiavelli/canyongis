"""Project-level hidden-asset table.

Adds ``canyongis_project_hidden_assets`` so users can remove an asset from a
project view without deleting the underlying asset or job. Hidden rows are
filtered out of ``list_project_assets`` and the project pipeline; the Asset
Library still shows them and lets the user un-hide / re-add them.

Revision ID: 0005_project_hidden_assets
Revises: 0004_asset_graph
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision = "0005_project_hidden_assets"
down_revision = "0004_asset_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE canyongis_project_hidden_assets (
            project_id text NOT NULL REFERENCES canyongis_projects(id) ON DELETE CASCADE,
            asset_id   text NOT NULL REFERENCES canyongis_assets(id)   ON DELETE CASCADE,
            hidden_by_user_id text REFERENCES canyongis_users(id),
            hidden_at  text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text,
            PRIMARY KEY (project_id, asset_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX canyongis_project_hidden_assets_asset_id_idx "
        "ON canyongis_project_hidden_assets (asset_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS canyongis_project_hidden_assets")

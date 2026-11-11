"""Which tile layers each canyon's map artifact holds, and when each was built.

``layers`` is {table: {"version": n, "built_at": iso}} copied from the artifact's own
``tile_stamps``; ``attempts`` is {table: iso} of the last try at a layer that could not be built
(no DEM, no aerial imagery), so it is not retried on every request.

Revision ID: 0011_canyon_tile_layers
Revises: 0010_canyon_forecast_hours
Create Date: 2026-11-10
"""

from __future__ import annotations

from alembic import op

revision = "0011_canyon_tile_layers"
down_revision = "0010_canyon_forecast_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE canyon.tile_builds
            ADD COLUMN IF NOT EXISTS layers   jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS attempts jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE canyon.tile_builds DROP COLUMN IF EXISTS layers, DROP COLUMN IF EXISTS attempts"
    )

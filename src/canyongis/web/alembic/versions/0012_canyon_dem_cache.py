"""Where each canyon's cropped DEM and the raw DEM tiles it was made from are kept.

``dem_path`` is the canyon's own cropped, compressed DEM (the area the map covers, so map layers
can be rebuilt without downloading anything). ``dem_sources`` lists the raw 1 m DEM tiles in the
shared source cache that it was cut from: tiles are shared between canyons that are close
together, and a later cleanup may delete one once no canyon without a cropped DEM needs it.

Revision ID: 0012_canyon_dem_cache
Revises: 0011_canyon_tile_layers
Create Date: 2026-11-10
"""

from __future__ import annotations

from alembic import op

revision = "0012_canyon_dem_cache"
down_revision = "0011_canyon_tile_layers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE canyon.tile_builds
            ADD COLUMN IF NOT EXISTS dem_path    text,
            ADD COLUMN IF NOT EXISTS dem_sources jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE canyon.tile_builds DROP COLUMN IF EXISTS dem_path,"
        " DROP COLUMN IF EXISTS dem_sources"
    )

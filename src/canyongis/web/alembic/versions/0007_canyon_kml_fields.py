"""Canyon KML fields: line colour, node names and descriptions (GeoPackage schema v4).

Revision ID: 0007_canyon_kml_fields
Revises: 0006_canyon_schema
Create Date: 2026-11-10
"""

from __future__ import annotations

from alembic import op

revision = "0007_canyon_kml_fields"
down_revision = "0006_canyon_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE canyon.routes ADD COLUMN IF NOT EXISTS color text")
    op.execute("ALTER TABLE canyon.waypoints ADD COLUMN IF NOT EXISTS description text")
    op.execute("ALTER TABLE canyon.rappels ADD COLUMN IF NOT EXISTS kml_name text")
    op.execute("ALTER TABLE canyon.rappels ADD COLUMN IF NOT EXISTS kml_description text")


def downgrade() -> None:
    op.execute("ALTER TABLE canyon.routes DROP COLUMN IF EXISTS color")
    op.execute("ALTER TABLE canyon.waypoints DROP COLUMN IF EXISTS description")
    op.execute("ALTER TABLE canyon.rappels DROP COLUMN IF EXISTS kml_name")
    op.execute("ALTER TABLE canyon.rappels DROP COLUMN IF EXISTS kml_description")

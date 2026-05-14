"""Backfill the local user, the Personal project, and existing jobs/assets.

For every distinct ``output_root`` present in canyongis_jobs we create a
Personal project owned by the local user, and attribute every legacy job to
that project. Asset rows are derived from the existing ``result`` JSON via
``canyongis.web.assets.extract_asset_candidates``.

Revision ID: 0003_backfill_local_user
Revises: 0002_multi_user_tables
Create Date: 2026-05-13
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import bcrypt
from alembic import op
from sqlalchemy import text

from canyongis.web.assets import extract_asset_candidates, new_asset_id

revision = "0003_backfill_local_user"
down_revision = "0002_multi_user_tables"
branch_labels = None
depends_on = None

LOCAL_USERNAME = "local"
LOCAL_PASSWORD_ENV = "CANYONGIS_LOCAL_PASSWORD"
PERSONAL_PROJECT_NAME = "Personal"


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Ensure the local user exists.
    row = conn.execute(
        text("SELECT id FROM canyongis_users WHERE username = :u"),
        {"u": LOCAL_USERNAME},
    ).fetchone()
    if row is None:
        password = os.environ.get(LOCAL_PASSWORD_ENV, "local")
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
        local_user_id = _new_id()
        conn.execute(
            text(
                "INSERT INTO canyongis_users (id, username, password_hash) "
                "VALUES (:id, :u, :h)"
            ),
            {"id": local_user_id, "u": LOCAL_USERNAME, "h": hashed},
        )
    else:
        local_user_id = row[0]

    # 2. Create one Personal project per distinct output_root, owned by local user.
    output_roots = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT DISTINCT output_root FROM canyongis_jobs "
                "WHERE project_id IS NULL"
            )
        ).fetchall()
    ]
    # Always ensure a default Personal project exists, even if there are no
    # legacy jobs to migrate.
    if not output_roots:
        output_roots = [""]

    project_by_root: dict[str, str] = {}
    for output_root in output_roots:
        existing = conn.execute(
            text(
                "SELECT p.id FROM canyongis_projects p "
                "JOIN canyongis_project_memberships m ON m.project_id = p.id "
                "WHERE m.user_id = :u AND p.name = :n LIMIT 1"
            ),
            {"u": local_user_id, "n": PERSONAL_PROJECT_NAME},
        ).fetchone()
        if existing is not None and output_root in project_by_root:
            project_by_root[output_root] = existing[0]
            continue
        project_id = existing[0] if existing is not None else _new_id()
        if existing is None:
            conn.execute(
                text(
                    "INSERT INTO canyongis_projects (id, name, created_by_user_id) "
                    "VALUES (:id, :n, :u)"
                ),
                {"id": project_id, "n": PERSONAL_PROJECT_NAME, "u": local_user_id},
            )
            conn.execute(
                text(
                    "INSERT INTO canyongis_project_memberships "
                    "(project_id, user_id, role) VALUES (:p, :u, 'owner')"
                ),
                {"p": project_id, "u": local_user_id},
            )
        project_by_root[output_root] = project_id

    # 3. Attribute every legacy job to the matching Personal project.
    for output_root, project_id in project_by_root.items():
        conn.execute(
            text(
                "UPDATE canyongis_jobs SET project_id = :p, created_by_user_id = :u "
                "WHERE output_root = :r AND project_id IS NULL"
            ),
            {"p": project_id, "u": local_user_id, "r": output_root},
        )

    # 4. Backfill asset rows from each job's result JSON.
    jobs = conn.execute(
        text(
            "SELECT id, project_id, output_root, result, created_at "
            "FROM canyongis_jobs WHERE result IS NOT NULL AND project_id IS NOT NULL"
        )
    ).fetchall()
    for job_id, project_id, output_root, result, created_at in jobs:
        if not output_root:
            continue
        result_dict = result if isinstance(result, dict) else json.loads(result)
        candidates = extract_asset_candidates(
            result_dict, output_root=Path(output_root)
        )
        for cand in candidates:
            already = conn.execute(
                text(
                    "SELECT 1 FROM canyongis_assets "
                    "WHERE project_id = :p AND path = :path LIMIT 1"
                ),
                {"p": project_id, "path": cand.path},
            ).fetchone()
            if already is not None:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO canyongis_assets
                        (id, project_id, job_id, created_by_user_id, kind, path,
                         url, bounds_wgs84, format, media_type, metadata, created_at)
                    VALUES
                        (:id, :p, :j, :u, :kind, :path, :url,
                         CAST(:bounds AS jsonb), :format, :media_type,
                         CAST(:metadata AS jsonb), :created_at)
                    """
                ),
                {
                    "id": new_asset_id(),
                    "p": project_id,
                    "j": job_id,
                    "u": local_user_id,
                    "kind": cand.kind,
                    "path": cand.path,
                    "url": cand.url,
                    "bounds": json.dumps(cand.metadata.get("bounds_wgs84")),
                    "format": cand.format,
                    "media_type": cand.media_type,
                    "metadata": json.dumps(cand.metadata),
                    "created_at": created_at,
                },
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DELETE FROM canyongis_assets"))
    conn.execute(text("UPDATE canyongis_jobs SET project_id = NULL, created_by_user_id = NULL"))
    conn.execute(text("DELETE FROM canyongis_project_memberships"))
    conn.execute(text("DELETE FROM canyongis_projects"))
    conn.execute(
        text("DELETE FROM canyongis_users WHERE username = :u"),
        {"u": LOCAL_USERNAME},
    )

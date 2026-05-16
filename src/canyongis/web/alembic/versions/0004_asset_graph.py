"""Asset dependency graph: typed asset kinds, project links, job input edges.

Adds two tables — ``canyongis_project_asset_links`` (m2m for cross-project
asset sharing) and ``canyongis_job_inputs`` (typed input edges
``(consumer_job, input_key, producer_asset)``).

Also reclassifies the existing ``canyongis_assets.kind`` column from
extension-derived strings ("tif", "kml", …) to the dotted taxonomy described
in ``UI_DESIGN.md`` §2.2 ("raster.dem", "vector.kml.route", …). New jobs
will write the typed kind via ``OutputSpec.asset_type``; the inline mapping
below covers every plugin in the registry at the time this migration was
written and is what runs against legacy rows.

Revision ID: 0004_asset_graph
Revises: 0003_backfill_local_user
Create Date: 2026-05-15
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0004_asset_graph"
down_revision = "0003_backfill_local_user"
branch_labels = None
depends_on = None

# (job_kind, result_key) -> typed asset kind. Keep in sync with OutputSpec
# .asset_type in each plugin module.
ASSET_KIND_MAP: dict[tuple[str, str], str] = {
    ("build-dem", "dem_path"): "raster.dem",
    ("hillshade", "hillshade_tif_path"): "raster.hillshade",
    ("drainage", "flow_accum_path"): "raster.flow_accumulation",
    ("geomorphon-ravine", "a_geomorphon_path"): "raster.score.ravine.geomorphon",
    ("tpi-ravine", "b_tpi_path"): "raster.score.ravine.tpi",
    ("curvature", "c_curvature_path"): "raster.score.ravine.curvature",
    ("kml-import", "kml_path"): "vector.kml.route",
    ("kml-snap", "kml_path"): "vector.kml.route.snapped",
    ("kml-snap", "snapped_gpkg_path"): "vector.gpkg.route.snapped",
    ("ravine-spine", "spines_path"): "vector.kml.spine",
    ("ravine-spine", "rappel_candidates_path"): "vector.kml.rappel_candidates",
}

# Prefix-rule for RVT (whose output keys are templated rvt_<vis>_path).
RVT_PREFIX = "rvt_"
RVT_SUFFIX = "_path"


# Legacy fall-back fields the dispatcher used before params._inputs existed.
# Maps result/param field name -> (input_key, accepted_kind_prefix).
LEGACY_INPUT_FIELDS: dict[str, tuple[str, str]] = {
    "dem_job_id": ("dem", "raster.dem"),
    "tpi_job_id": ("score", "raster.score."),
    "compare_job_id": ("score", "raster.score."),
    "kml_job_id": ("kml", "vector.kml."),
    "parent_kml_job_id": ("parent_kml", "vector.kml."),
}


def _typed_kind(job_kind: str, result_key: str) -> str | None:
    if (job_kind, result_key) in ASSET_KIND_MAP:
        return ASSET_KIND_MAP[(job_kind, result_key)]
    if job_kind == "rvt" and result_key.startswith(RVT_PREFIX) and result_key.endswith(RVT_SUFFIX):
        vis = result_key[len(RVT_PREFIX) : -len(RVT_SUFFIX)]
        if vis:
            return f"raster.relief.{vis}"
    return None


def _result_path_for_asset(job_result: dict | None, asset_path: str) -> str | None:
    """Find the result-dict key whose value equals this asset's path."""
    if not isinstance(job_result, dict):
        return None
    resolved = str(Path(asset_path).resolve())
    for key, value in job_result.items():
        if not isinstance(value, str) or not value:
            continue
        try:
            if str(Path(value).resolve()) == resolved:
                return key
        except OSError:
            continue
    return None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE canyongis_project_asset_links (
            project_id text NOT NULL REFERENCES canyongis_projects(id) ON DELETE CASCADE,
            asset_id   text NOT NULL REFERENCES canyongis_assets(id)   ON DELETE CASCADE,
            linked_by_user_id text REFERENCES canyongis_users(id),
            linked_at  text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text,
            PRIMARY KEY (project_id, asset_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX canyongis_project_asset_links_asset_id_idx "
        "ON canyongis_project_asset_links (asset_id)"
    )

    op.execute(
        """
        CREATE TABLE canyongis_job_inputs (
            job_id    text NOT NULL REFERENCES canyongis_jobs(id) ON DELETE CASCADE,
            input_key text NOT NULL,
            asset_id  text NOT NULL REFERENCES canyongis_assets(id) ON DELETE CASCADE,
            PRIMARY KEY (job_id, input_key, asset_id)
        )
        """
    )
    op.execute("CREATE INDEX canyongis_job_inputs_asset_id_idx ON canyongis_job_inputs (asset_id)")

    conn = op.get_bind()

    # 1. Reclassify existing canyongis_assets.kind to the typed taxonomy.
    #    Walk every asset, look up the producing job's result dict, find which
    #    result-dict key references this asset's path, and map (job.kind, key)
    #    via ASSET_KIND_MAP. Leaves the old kind in place if no rule matches.
    assets = conn.execute(
        text(
            "SELECT a.id, a.path, j.kind, j.result "
            "FROM canyongis_assets a "
            "LEFT JOIN canyongis_jobs j ON j.id = a.job_id"
        )
    ).fetchall()
    for asset_id, path, job_kind, job_result in assets:
        if not job_kind:
            continue
        result_dict = (
            job_result
            if isinstance(job_result, dict)
            else (json.loads(job_result) if isinstance(job_result, str) and job_result else None)
        )
        result_key = _result_path_for_asset(result_dict, path)
        if result_key is None:
            continue
        typed = _typed_kind(job_kind, result_key)
        if typed is None:
            continue
        conn.execute(
            text("UPDATE canyongis_assets SET kind = :k WHERE id = :id"),
            {"k": typed, "id": asset_id},
        )

    # 2. Backfill canyongis_job_inputs from existing jobs.
    #    Source 1: params._inputs (recorded by current dispatcher).
    #    Source 2: legacy fields in params / result (dem_job_id, tpi_job_id, …).
    jobs = conn.execute(
        text(
            "SELECT id, params, result, project_id FROM canyongis_jobs "
            "WHERE status = 'succeeded' AND project_id IS NOT NULL"
        )
    ).fetchall()
    for job_id, params, result, _project_id in jobs:
        params_dict = (
            params
            if isinstance(params, dict)
            else (json.loads(params) if isinstance(params, str) and params else {})
        )
        result_dict = (
            result
            if isinstance(result, dict)
            else (json.loads(result) if isinstance(result, str) and result else {})
        )
        edges: dict[tuple[str, str], str] = {}  # (input_key, asset_id) dedup

        recorded = params_dict.get("_inputs") if isinstance(params_dict, dict) else None
        if isinstance(recorded, dict):
            for input_key, raw in recorded.items():
                producer_job_id = _coerce_job_id(raw)
                if not producer_job_id:
                    continue
                asset_id = _pick_producer_asset(conn, producer_job_id, accepted_prefix=None)
                if asset_id:
                    edges[(input_key, asset_id)] = asset_id

        for field, (input_key, kind_prefix) in LEGACY_INPUT_FIELDS.items():
            source = params_dict.get(field) if isinstance(params_dict, dict) else None
            if not isinstance(source, str) or not source:
                source = result_dict.get(field) if isinstance(result_dict, dict) else None
            if not isinstance(source, str) or not source:
                continue
            asset_id = _pick_producer_asset(conn, source, accepted_prefix=kind_prefix)
            if asset_id:
                edges.setdefault((input_key, asset_id), asset_id)

        for (input_key, asset_id), _ in edges.items():
            conn.execute(
                text(
                    "INSERT INTO canyongis_job_inputs (job_id, input_key, asset_id) "
                    "VALUES (:j, :k, :a) ON CONFLICT DO NOTHING"
                ),
                {"j": job_id, "k": input_key, "a": asset_id},
            )


def _coerce_job_id(raw) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for k in ("job_id", "id"):
            v = raw.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _pick_producer_asset(conn, producer_job_id: str, *, accepted_prefix: str | None) -> str | None:
    """Pick the best asset produced by ``producer_job_id`` matching the kind prefix."""
    if accepted_prefix:
        row = conn.execute(
            text(
                "SELECT id FROM canyongis_assets "
                "WHERE job_id = :j AND kind LIKE :p "
                "ORDER BY created_at LIMIT 1"
            ),
            {"j": producer_job_id, "p": f"{accepted_prefix}%"},
        ).fetchone()
        if row is not None:
            return row[0]
    row = conn.execute(
        text("SELECT id FROM canyongis_assets WHERE job_id = :j ORDER BY created_at LIMIT 1"),
        {"j": producer_job_id},
    ).fetchone()
    return row[0] if row is not None else None


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS canyongis_job_inputs")
    op.execute("DROP TABLE IF EXISTS canyongis_project_asset_links")
    # Asset kind reclassification is not reversed — old extension-derived
    # values are no longer trustworthy and re-deriving them would require
    # rerunning extract_asset_candidates. The kind column tolerates either
    # taxonomy.

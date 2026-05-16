"""Tests for the Phase A asset graph: typed kinds, edges, lineage API.

Covers UI_DESIGN.md Phase A — the backend-only changes that make the typed
asset taxonomy and ``canyongis_job_inputs`` edges observable.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import BaseModel

from canyongis.web.dem_jobs import DemJobSpec, OutputSpec, get_spec, register
from canyongis.web.server import Job, create_app

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

PRODUCER_KEY = "test-asset-graph-producer"
CONSUMER_KEY = "test-asset-graph-consumer"


class _NoParams(BaseModel):
    tag: str = "x"


def _producer_runner(*, dem_path: str, out_dir: Path, req: _NoParams, progress_cb, output_url):
    path = out_dir / f"producer-{req.tag}.tif"
    path.write_bytes(b"fake")
    return {
        "dem_path": dem_path,
        "out_dir": str(out_dir),
        "bounds_wgs84": [1.0, 2.0, 3.0, 4.0],
        "score_path": str(path),
    }


def _consumer_runner(*, dem_path: str, out_dir: Path, req: _NoParams, progress_cb, output_url):
    path = out_dir / f"consumer-{req.tag}.kml"
    path.write_text("<kml/>")
    return {
        "dem_path": dem_path,
        "out_dir": str(out_dir),
        "bounds_wgs84": [1.0, 2.0, 3.0, 4.0],
        "spines_path": str(path),
    }


if get_spec(PRODUCER_KEY) is None:
    register(
        DemJobSpec(
            key=PRODUCER_KEY,
            label="Test asset-graph producer",
            description="Emits a typed score raster used to chain into the consumer.",
            request_model=_NoParams,
            visualizations=(),
            runner=_producer_runner,
            outputs=(
                OutputSpec(
                    key="score",
                    label="Score",
                    path_field="score_path",
                    role="ravine-score",
                    asset_kind="raster",
                    asset_type="raster.score.ravine.tpi",
                ),
            ),
        )
    )

if get_spec(CONSUMER_KEY) is None:
    register(
        DemJobSpec(
            key=CONSUMER_KEY,
            label="Test asset-graph consumer",
            description="Consumes a score raster, emits spines.",
            request_model=_NoParams,
            visualizations=(),
            runner=_consumer_runner,
            outputs=(
                OutputSpec(
                    key="spines",
                    label="Spines",
                    path_field="spines_path",
                    role="route",
                    asset_kind="vector",
                    asset_type="vector.kml.spine",
                ),
            ),
        )
    )


def _insert_dem_job(ctx, *, job_id: str) -> Job:
    user = ctx.auth.local_user()
    assert user is not None
    project_id = ctx.store.ensure_personal_project(user)
    dem_path = ctx.output_root / "dems" / f"{job_id}.tif"
    dem_path.parent.mkdir(parents=True, exist_ok=True)
    dem_path.write_bytes(b"fake-dem")
    job = Job(
        id=job_id,
        kind="build-dem",
        status="succeeded",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:00+00:00",
        params={"name": job_id},
        result={
            "dem_path": str(dem_path),
            "bounds_wgs84": [1.0, 2.0, 3.0, 4.0],
            "outputs": [
                {
                    "key": "dem",
                    "label": "DEM",
                    "role": "dem",
                    "asset_kind": "raster",
                    "asset_type": "raster.dem",
                    "path": str(dem_path),
                    "metadata": {"bounds_wgs84": [1.0, 2.0, 3.0, 4.0]},
                }
            ],
        },
        project_id=project_id,
        created_by_user_id=user.id,
    )
    ctx.store.upsert(job)
    ctx.store.register_assets(job, project_id=project_id, created_by_user_id=user.id)
    return job


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(50):
        row = client.get(f"/api/jobs/{job_id}").json()
        if row["status"] in {"succeeded", "failed", "canceled"}:
            return row
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job_id}")


def _project_id(ctx) -> str:
    user = ctx.auth.local_user()
    return ctx.store.ensure_personal_project(user)


def _create_project(ctx, name: str) -> str:
    user = ctx.auth.local_user()
    assert user is not None
    project_id = f"project-{name}"
    with ctx.store.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO canyongis_projects (id, name, created_by_user_id) VALUES (%s, %s, %s)",
            (project_id, name, user.id),
        )
        cur.execute(
            "INSERT INTO canyongis_project_memberships (project_id, user_id, role) "
            "VALUES (%s, %s, 'owner')",
            (project_id, user.id),
        )
    return project_id


def test_migration_creates_asset_graph_tables(live_postgres) -> None:
    from canyongis.web.db import apply_migrations

    apply_migrations(live_postgres)
    with psycopg.connect(**live_postgres.connect_kwargs()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'canyongis_job_inputs'"
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'canyongis_project_asset_links'"
        )
        assert cur.fetchone() is not None


def test_typed_asset_kind_lands_on_canyongis_assets(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    dem = _insert_dem_job(ctx, job_id="dem-typed")

    res = client.post(
        f"/api/jobs/{PRODUCER_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "scored"}},
    )
    assert res.status_code == 200, res.text
    row = _wait_for_job(client, res.json()["id"])
    assert row["status"] == "succeeded", row.get("error")

    with ctx.store.conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM canyongis_assets WHERE job_id = %s ORDER BY created_at",
            (row["id"],),
        )
        kinds = [r[0] for r in cur.fetchall()]
    assert "raster.score.ravine.tpi" in kinds


def test_consumer_job_records_typed_edge(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    dem = _insert_dem_job(ctx, job_id="dem-edge")

    producer = client.post(
        f"/api/jobs/{PRODUCER_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "p"}},
    )
    assert producer.status_code == 200, producer.text
    producer_row = _wait_for_job(client, producer.json()["id"])
    assert producer_row["status"] == "succeeded", producer_row.get("error")

    consumer = client.post(
        f"/api/jobs/{CONSUMER_KEY}",
        json={
            "inputs": {"dem": dem.id, "score": producer_row["id"]},
            "params": {"tag": "c"},
        },
    )
    assert consumer.status_code == 200, consumer.text
    consumer_row = _wait_for_job(client, consumer.json()["id"])
    assert consumer_row["status"] == "succeeded", consumer_row.get("error")

    with ctx.store.conn.cursor() as cur:
        cur.execute(
            "SELECT input_key FROM canyongis_job_inputs WHERE job_id = %s ORDER BY input_key",
            (consumer_row["id"],),
        )
        keys = [r[0] for r in cur.fetchall()]
    assert "dem" in keys
    assert "score" in keys


def test_list_project_assets_filters_by_kind_prefix(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    project_id = _project_id(ctx)
    dem = _insert_dem_job(ctx, job_id="dem-list")

    producer = client.post(
        f"/api/jobs/{PRODUCER_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "f"}},
    )
    assert producer.status_code == 200, producer.text
    _wait_for_job(client, producer.json()["id"])

    all_assets = client.get(f"/api/projects/{project_id}/assets").json()["assets"]
    kinds = {a["kind"] for a in all_assets}
    assert "raster.dem" in kinds
    assert "raster.score.ravine.tpi" in kinds

    dems = client.get(f"/api/projects/{project_id}/assets", params={"kind": "raster.dem"}).json()[
        "assets"
    ]
    assert {a["kind"] for a in dems} == {"raster.dem"}

    scores = client.get(
        f"/api/projects/{project_id}/assets", params={"kind": "raster.score"}
    ).json()["assets"]
    assert {a["kind"] for a in scores} == {"raster.score.ravine.tpi"}


def test_kml_import_container_records_dem_edge(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    ctx = app.state.ctx
    dem = _insert_dem_job(ctx, job_id="dem-container")
    assert dem.project_id is not None
    kml_path = ctx.output_root / "kml" / "kml-contained" / "route.kml"
    kml_path.parent.mkdir(parents=True, exist_ok=True)
    kml_path.write_text("<kml/>")
    job = Job(
        id="kml-contained",
        kind="kml-import",
        status="succeeded",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:00+00:00",
        params={"_inputs": {"container": dem.id}, "container_dem_job_id": dem.id},
        result={
            "name": "Contained Route",
            "container_dem_job_id": dem.id,
            "kml_path": str(kml_path),
            "bounds_wgs84": [1.1, 2.1, 3.1, 4.1],
        },
        project_id=dem.project_id,
        created_by_user_id=dem.created_by_user_id,
    )
    ctx.store.upsert(job)
    ctx.store.register_assets(
        job, project_id=dem.project_id, created_by_user_id=dem.created_by_user_id
    )

    assert ctx.store.register_job_inputs(job) == 1

    with ctx.store.conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.input_key, producer.job_id, consumer.job_id
            FROM canyongis_job_inputs i
            JOIN canyongis_assets producer ON producer.id = i.asset_id
            JOIN canyongis_assets consumer ON consumer.job_id = i.job_id
            WHERE i.job_id = %s
            """,
            (job.id,),
        )
        assert cur.fetchall() == [("container", dem.id, job.id)]


def test_asset_lineage_returns_full_chain(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    dem = _insert_dem_job(ctx, job_id="dem-lineage")

    producer = client.post(
        f"/api/jobs/{PRODUCER_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "lp"}},
    )
    producer_row = _wait_for_job(client, producer.json()["id"])
    assert producer_row["status"] == "succeeded", producer_row.get("error")

    consumer = client.post(
        f"/api/jobs/{CONSUMER_KEY}",
        json={
            "inputs": {"dem": dem.id, "score": producer_row["id"]},
            "params": {"tag": "lc"},
        },
    )
    consumer_row = _wait_for_job(client, consumer.json()["id"])
    assert consumer_row["status"] == "succeeded", consumer_row.get("error")

    # Pick the spines asset and walk its lineage upward.
    with ctx.store.conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM canyongis_assets WHERE job_id = %s AND kind = %s",
            (consumer_row["id"], "vector.kml.spine"),
        )
        spines_asset_id = cur.fetchone()[0]

    lineage = client.get(f"/api/assets/{spines_asset_id}/lineage").json()
    assert lineage["root"] == spines_asset_id
    kinds = {node["kind"] for node in lineage["nodes"].values()}
    assert "vector.kml.spine" in kinds
    assert "raster.score.ravine.tpi" in kinds
    assert "raster.dem" in kinds
    input_keys = {edge["input_key"] for edge in lineage["edges"]}
    assert {"dem", "score"}.issubset(input_keys)


def test_project_asset_link_and_unlink(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    source_project_id = _project_id(ctx)
    target_project_id = _create_project(ctx, "linked")
    dem = _insert_dem_job(ctx, job_id="dem-link")

    with ctx.store.conn.cursor() as cur:
        cur.execute("SELECT id FROM canyongis_assets WHERE job_id = %s", (dem.id,))
        asset_id = cur.fetchone()[0]

    link = client.post(
        f"/api/projects/{target_project_id}/assets/link",
        json={"asset_id": asset_id},
    )
    assert link.status_code == 200, link.text
    assert link.json()["linked"] is True

    source_assets = client.get(f"/api/projects/{source_project_id}/assets").json()["assets"]
    target_assets = client.get(f"/api/projects/{target_project_id}/assets").json()["assets"]
    assert any(a["id"] == asset_id and not a["linked"] for a in source_assets)
    assert any(a["id"] == asset_id and a["linked"] for a in target_assets)

    unlink = client.delete(f"/api/projects/{target_project_id}/assets/{asset_id}")
    assert unlink.status_code == 200, unlink.text
    assert unlink.json()["deleted"] is True
    target_assets = client.get(f"/api/projects/{target_project_id}/assets").json()["assets"]
    assert all(a["id"] != asset_id for a in target_assets)


def test_project_pipeline_returns_visible_asset_dag(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    project_id = _project_id(ctx)
    dem = _insert_dem_job(ctx, job_id="dem-pipeline")

    producer = client.post(
        f"/api/jobs/{PRODUCER_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "pipe"}},
    )
    producer_row = _wait_for_job(client, producer.json()["id"])
    assert producer_row["status"] == "succeeded", producer_row.get("error")

    pipeline = client.get(f"/api/projects/{project_id}/pipeline").json()
    kinds = {asset["kind"] for asset in pipeline["assets"]}
    assert "raster.dem" in kinds
    assert "raster.score.ravine.tpi" in kinds
    assert any(edge["input_key"] == "dem" for edge in pipeline["edges"])

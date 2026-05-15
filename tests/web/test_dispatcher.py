from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from canyongis.web.dem_jobs import DemJobSpec, OutputSpec, get_spec, register
from canyongis.web.server import Job, create_app

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

TEST_TOOL_KEY = "test-phase2-dispatch"


class FakeRequest(BaseModel):
    tag: str = "default"


def _fake_runner(
    *,
    dem_path: str,
    out_dir: Path,
    req: FakeRequest,
    progress_cb,
    output_url,
) -> dict[str, Any]:
    progress_cb(f"fake dispatch {req.tag}")
    path = out_dir / f"{req.tag}.txt"
    path.write_text(dem_path)
    return {
        "dem_path": dem_path,
        "out_dir": str(out_dir),
        "bounds_wgs84": [1.0, 2.0, 3.0, 4.0],
        "fake_path": str(path),
        "fake_url": output_url(path),
    }


if get_spec(TEST_TOOL_KEY) is None:
    register(
        DemJobSpec(
            key=TEST_TOOL_KEY,
            label="Test phase 2 dispatch",
            description="Test-only fake DEM-derived job.",
            request_model=FakeRequest,
            visualizations=(),
            runner=_fake_runner,
            outputs=(
                OutputSpec(
                    key="fake",
                    label="Fake output",
                    path_field="fake_path",
                    role="test-output",
                    asset_kind="text",
                    url_field="fake_url",
                ),
            ),
        )
    )


def _insert_dem_job(ctx, tmp_path: Path, *, job_id: str, created_at: str) -> Job:
    user = ctx.auth.local_user()
    assert user is not None
    project_id = ctx.store.ensure_personal_project(user)
    dem_path = tmp_path / f"{job_id}.tif"
    dem_path.write_bytes(b"fake-dem")
    job = Job(
        id=job_id,
        kind="build-dem",
        status="succeeded",
        created_at=created_at,
        updated_at=created_at,
        params={"name": job_id},
        result={"dem_path": str(dem_path), "bounds_wgs84": [1.0, 2.0, 3.0, 4.0]},
        project_id=project_id,
        created_by_user_id=user.id,
    )
    ctx.store.upsert(job)
    return job


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(50):
        row = client.get(f"/api/jobs/{job_id}").json()
        if row["status"] in {"succeeded", "failed", "canceled"}:
            return row
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job_id}")


def test_generic_tool_endpoint_defaults_to_latest_dem(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    ctx = app.state.ctx
    _insert_dem_job(ctx, tmp_path, job_id="old-dem", created_at="2026-01-01T00:00:00+00:00")
    new_dem = _insert_dem_job(
        ctx, tmp_path, job_id="new-dem", created_at="2026-01-02T00:00:00+00:00"
    )

    res = client.post(f"/api/jobs/{TEST_TOOL_KEY}", json={"params": {"tag": "generic"}})

    assert res.status_code == 200, res.text
    row = _wait_for_job(client, res.json()["id"])
    assert row["status"] == "succeeded", row.get("error")
    assert row["params"]["dem_job_id"] == new_dem.id
    assert new_dem.result is not None
    assert row["result"]["dem_path"] == new_dem.result["dem_path"]
    assert row["result"]["outputs"][0]["role"] == "test-output"


def test_legacy_dem_run_endpoint_uses_shared_dispatcher(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    dem = _insert_dem_job(
        app.state.ctx, tmp_path, job_id="explicit-dem", created_at="2026-01-01T00:00:00+00:00"
    )

    res = client.post(f"/api/jobs/{dem.id}/run/{TEST_TOOL_KEY}", json={"tag": "legacy"})

    assert res.status_code == 200, res.text
    row = _wait_for_job(client, res.json()["id"])
    assert row["status"] == "succeeded", row.get("error")
    assert row["params"]["dem_job_id"] == dem.id
    assert row["params"]["job_key"] == TEST_TOOL_KEY
    assert row["result"]["fake_url"].endswith("/legacy.txt")


def test_generic_tool_endpoint_reports_missing_dem(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)

    res = client.post(f"/api/jobs/{TEST_TOOL_KEY}", json={"params": {}})

    assert res.status_code == 400
    assert "DEM input is required" in res.json()["detail"]

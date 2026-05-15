from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from canyongis.aoi_mosaic import AOIMosaicResult
from canyongis.web import source_jobs
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


def test_build_dem_tool_runs_through_dispatcher(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")

    def fake_build(bounds, out_path, *, cache_dir, buffer_m, max_products):
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-dem")
        return AOIMosaicResult(
            dem_path=str(path),
            source_paths=[],
            bounds_wgs84=bounds,
            crs="EPSG:32612",
            pixel_size_m=1.0,
            width=4,
            height=4,
        )

    monkeypatch.setattr(source_jobs, "build_aoi_dem_mosaic", fake_build)
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)

    res = client.post(
        "/api/jobs/build-dem",
        json={
            "params": {
                "name": "dispatcher-aoi",
                "bounds": {"west": -113.0, "south": 37.0, "east": -112.0, "north": 38.0},
                "buffer_m": 0,
            }
        },
    )

    assert res.status_code == 200, res.text
    row = _wait_for_job(client, res.json()["id"])
    assert row["status"] == "succeeded", row.get("error")
    assert row["kind"] == "build-dem"
    assert row["result"]["outputs"][0]["role"] == "dem"
    assert row["result"]["layers"][0]["kind"] == "footprint"


def test_source_tools_are_exposed_in_specs_and_tools(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)

    specs = {spec["key"]: spec for spec in client.get("/api/job-specs").json()["specs"]}
    tools = client.get("/api/tools").json()["tools"]

    assert specs["build-dem"]["outputs"][0]["role"] == "dem"
    assert specs["kml-import"]["layers"][0]["kind"] == "vector-kml"
    assert sum(1 for tool in tools if tool["id"] == "build-dem") == 1

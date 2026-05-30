from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from canyongis.aoi_mosaic import AOIMosaicResult
from canyongis.web import mcp_server, source_jobs
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


def test_generic_tool_endpoint_with_explicit_dem(
    live_postgres, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)
    dem = _insert_dem_job(
        app.state.ctx, tmp_path, job_id="explicit-dem", created_at="2026-01-01T00:00:00+00:00"
    )

    res = client.post(
        f"/api/jobs/{TEST_TOOL_KEY}",
        json={"inputs": {"dem": dem.id}, "params": {"tag": "explicit"}},
    )

    assert res.status_code == 200, res.text
    row = _wait_for_job(client, res.json()["id"])
    assert row["status"] == "succeeded", row.get("error")
    assert row["params"]["dem_job_id"] == dem.id
    assert row["params"]["job_key"] == TEST_TOOL_KEY
    assert row["result"]["fake_url"].endswith("/explicit.txt")


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
    assert specs["tpi-ravine"]["runner_kind"] == "dem"
    assert specs["build-dem"]["runner_kind"] == "context"
    assert specs["kml-import"]["layers"][0]["kind"] == "vector-kml"
    assert specs["kml-snap"]["inputs"][0]["key"] == "kml"
    assert specs["track-profile"]["inputs"][1]["kind"] == "raster.dem"
    assert specs["ravine-spine"]["outputs"][0]["role"] == "route"
    assert sum(1 for tool in tools if tool["id"] == "build-dem") == 1


def test_kml_source_classifier_recognizes_url_types() -> None:
    assert source_jobs.classify_kml_source("https://ropewiki.com/Foo") == "ropewiki"
    assert source_jobs.classify_kml_source("https://caltopo.com/m/ABC") == "caltopo"
    assert source_jobs.classify_kml_source("https://example.com/route.kml") == "url"
    assert source_jobs.classify_kml_source("Foo_Canyon") == "ropewiki"


def test_caltopo_kml_url_rewrites_public_map_url() -> None:
    # CalTopo's public map URL has no usable .kml endpoint — fetching .kml redirects
    # to a login page — so we route to the JSON since/0 feed and convert features
    # to KML in-process. Also strip slug + #fragment when extracting the map ID.
    assert (
        source_jobs.caltopo_kml_url("https://caltopo.com/m/ABC123")
        == "https://caltopo.com/api/v1/map/ABC123/since/0"
    )
    assert (
        source_jobs.caltopo_kml_url("https://caltopo.com/m/ABC123/slug")
        == "https://caltopo.com/api/v1/map/ABC123/since/0"
    )
    # Regression: the URL the user hit in production: caltopo.com/m/<ID>#
    assert (
        source_jobs.caltopo_kml_url("https://caltopo.com/m/90P3LRQ#")
        == "https://caltopo.com/api/v1/map/90P3LRQ/since/0"
    )


def test_caltopo_features_to_kml_converts_points_and_lines() -> None:
    # Shape mirrors what caltopo.com/api/v1/map/<ID>/since/0 actually returns
    # (see real fetch against 90P3LRQ during development).
    sample = {
        "result": {
            "state": {
                "features": [
                    {
                        "geometry": {
                            "coordinates": [-117.7550809, 34.356111, 0, 0],
                            "type": "Point",
                        },
                        "type": "Feature",
                        "properties": {
                            "title": "Big Horn Mine",
                            "description": "Rock fall danger above",
                            "class": "Marker",
                        },
                    },
                    {
                        "geometry": {
                            "coordinates": [
                                [-117.75222, 34.37297],
                                [-117.75229, 34.37296],
                                [-117.75268, 34.37308],
                            ],
                            "type": "LineString",
                        },
                        "type": "Feature",
                        "properties": {"title": "Approach", "class": "Shape"},
                    },
                ]
            }
        }
    }
    kml = source_jobs._caltopo_features_to_kml(sample, document_name="90P3LRQ")
    text = kml.decode("utf-8")
    assert "<name>90P3LRQ</name>" in text
    assert "<name>Big Horn Mine</name>" in text
    assert "<description>Rock fall danger above</description>" in text
    assert "-117.7550809,34.356111,0" in text
    assert "<LineString>" in text
    assert "-117.75222,34.37297,0 -117.75229,34.37296,0 -117.75268,34.37308,0" in text


def test_caltopo_kml_import_uses_json_endpoint_and_writes_features_kml(
    tmp_path: Path, monkeypatch
) -> None:
    """Drive _run_ropewiki_kml_import with the CalTopo URL the user reported."""
    sample_json = json.dumps(
        {
            "result": {
                "state": {
                    "features": [
                        {
                            "geometry": {
                                "coordinates": [-117.755, 34.356],
                                "type": "Point",
                            },
                            "type": "Feature",
                            "properties": {"title": "Big Horn Mine"},
                        }
                    ]
                }
            }
        }
    ).encode("utf-8")
    captured: list[str] = []

    def fake_fetch(url: str, timeout: float = 120.0) -> bytes:
        captured.append(url)
        return sample_json

    monkeypatch.setattr(source_jobs, "_fetch_url_bytes", fake_fetch)
    monkeypatch.setattr(
        source_jobs,
        "_assign_kml_container_if_valid",
        lambda *a, **k: None,
    )

    ctx = SimpleNamespace(
        output_root=tmp_path,
        output_url=lambda p: f"file://{p}",
        job_or_404=lambda job_id: None,
    )
    req = source_jobs.RopewikiKmlImportRequest(source="https://caltopo.com/m/90P3LRQ#")

    result = source_jobs._run_ropewiki_kml_import(
        ctx=ctx,
        req=req,
        params={},
        progress_cb=lambda _msg: None,
        job_id="job-1",
    )

    assert captured == ["https://caltopo.com/api/v1/map/90P3LRQ/since/0"]
    assert result["source"] == {
        "type": "caltopo",
        "source": "https://caltopo.com/m/90P3LRQ#",
        "download_url": "https://caltopo.com/api/v1/map/90P3LRQ/since/0",
        "map_id": "90P3LRQ",
    }
    assert Path(result["kml_path"]).exists()
    assert result["counts"]["points"] == 1


def test_network_link_follower_chases_inner_href() -> None:
    network_link_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <NetworkLink>
    <name>Live</name>
    <Link>
      <href>https://example.com/real.kml?token=xyz</href>
      <refreshInterval>3600</refreshInterval>
    </Link>
  </NetworkLink>
</kml>"""
    actual_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml><Document><Placemark><Point><coordinates>0,0,0</coordinates></Point></Placemark></Document></kml>"""
    target = source_jobs._network_link_target(network_link_kml, "https://caltopo.com/m/ID.kml")
    assert target == "https://example.com/real.kml?token=xyz"
    # _fetch_kml_following_network_link should follow once and stop when the
    # second fetch yields a non-network-link KML.
    fetched: list[str] = []

    def fake_fetch(url: str, timeout: float = 120.0) -> bytes:
        fetched.append(url)
        return network_link_kml if url.startswith("https://caltopo.com/") else actual_kml

    orig = source_jobs._fetch_url_bytes
    source_jobs._fetch_url_bytes = fake_fetch
    try:
        content, final = source_jobs._fetch_kml_following_network_link(
            "https://caltopo.com/m/ID.kml", progress_cb=lambda _m: None
        )
    finally:
        source_jobs._fetch_url_bytes = orig
    assert fetched == [
        "https://caltopo.com/m/ID.kml",
        "https://example.com/real.kml?token=xyz",
    ]
    assert final == "https://example.com/real.kml?token=xyz"
    assert content == actual_kml


def test_bulk_ropewiki_import_names_children_and_preserves_container(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    parent = Job(
        id="bulk-job",
        kind="ropewiki-bulk-import",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        params={},
        project_id="project-1",
        created_by_user_id="user-1",
    )
    ctx = SimpleNamespace(job_or_404=lambda job_id: parent)

    def fake_submit_tool_job(wc, tool_key, body, *, user, project_id):
        calls.append({"tool_key": tool_key, "body": body, "user": user, "project_id": project_id})
        return Job(
            id="child-job",
            kind=tool_key,
            status="queued",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            params=body["params"],
            project_id=project_id,
            created_by_user_id=user.id,
        )

    monkeypatch.setattr(
        source_jobs,
        "list_ropewiki_canyons_in_bbox",
        lambda bounds, max_results: [
            {"title": "Fantastic Mr. Fox Canyon", "latitude": 37.1, "longitude": -112.9}
        ],
    )
    monkeypatch.setattr("canyongis.web.dispatcher.submit_tool_job", fake_submit_tool_job)

    result = source_jobs._run_bulk_ropewiki_kml_import(
        ctx=ctx,
        req=source_jobs.BulkRopewikiKmlImportRequest(
            bounds={"west": -113.0, "south": 37.0, "east": -112.0, "north": 38.0},
            container_dem_job_id="dem-container",
            max_results=10,
        ),
        inputs={},
        progress_cb=lambda _message: None,
        job_id=parent.id,
    )

    assert result["submitted_jobs"] == [
        {
            "page_title": "Fantastic Mr. Fox Canyon",
            "job_id": "child-job",
            "source": "https://ropewiki.com/Fantastic_Mr._Fox_Canyon",
            "latitude": 37.1,
            "longitude": -112.9,
        }
    ]
    assert calls[0]["body"] == {
        "params": {
            "source": "https://ropewiki.com/Fantastic_Mr._Fox_Canyon",
            "name": "Fantastic Mr. Fox Canyon",
            "container_dem_job_id": "dem-container",
        },
        "inputs": {"container": "dem-container"},
    }


def _patch_mcp_dispatch(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_submit_tool_job(wc, tool_key, body, *, user, project_id, legacy_dem_job_id=None):
        calls.append(
            {
                "wc": wc,
                "tool_key": tool_key,
                "body": body,
                "user": user,
                "project_id": project_id,
                "legacy_dem_job_id": legacy_dem_job_id,
            }
        )
        return Job(
            id=f"{tool_key}-job",
            kind=tool_key,
            status="queued",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            params=body,
            project_id=project_id,
            created_by_user_id=user.id,
        )

    monkeypatch.setattr(mcp_server, "ctx", lambda: object())
    monkeypatch.setattr(mcp_server, "_resolve_user", lambda: {"id": "u1", "username": "local"})
    monkeypatch.setattr(mcp_server, "_project_id", lambda user_info: "p1")
    monkeypatch.setattr(mcp_server, "submit_tool_job", fake_submit_tool_job)
    return calls


def test_mcp_snap_kml_uses_dispatcher(monkeypatch) -> None:
    calls = _patch_mcp_dispatch(monkeypatch)

    row = mcp_server.snap_kml.fn(
        "kml-1",
        dem_job_id="dem-1",
        target="dem",
        search_radius_m=12.0,
        sample_step_m=3.0,
    )

    assert row["id"] == "kml-snap-job"
    assert calls[0]["tool_key"] == "kml-snap"
    assert calls[0]["body"]["inputs"] == {"kml": "kml-1", "dem": "dem-1"}
    assert calls[0]["body"]["params"]["search_radius_m"] == 12.0


def test_mcp_track_profile_uses_dispatcher(monkeypatch) -> None:
    calls = _patch_mcp_dispatch(monkeypatch)

    row = mcp_server.track_profile.fn("kml-1", "dem-1", category="approach", track_index=2)

    assert row["id"] == "track-profile-job"
    assert calls[0]["tool_key"] == "track-profile"
    assert calls[0]["body"]["inputs"] == {"kml": "kml-1", "dem": "dem-1"}
    assert calls[0]["body"]["params"]["category"] == "approach"
    assert calls[0]["body"]["params"]["track_index"] == 2


def test_mcp_ravine_spine_uses_dispatcher(monkeypatch) -> None:
    calls = _patch_mcp_dispatch(monkeypatch)

    row = mcp_server.ravine_spine.fn("tpi-1", threshold_pct=96.0, parent_kml_job_id="kml-1")

    assert row["id"] == "ravine-spine-job"
    assert calls[0]["tool_key"] == "ravine-spine"
    assert calls[0]["body"]["inputs"] == {"score": "tpi-1"}
    assert calls[0]["body"]["params"]["threshold_pct"] == 96.0
    assert calls[0]["body"]["params"]["parent_kml_job_id"] == "kml-1"


def test_generic_analysis_dispatch(live_postgres, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANYONGIS_SINGLE_USER", "1")
    calls: list[dict[str, Any]] = []

    def fake_submit_tool_job(ctx, tool_key, body, *, user, project_id):
        calls.append({"tool_key": tool_key, "body": body})
        return Job(
            id=f"{tool_key}-job",
            kind=tool_key,
            status="queued",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            params=body,
            project_id=project_id,
            created_by_user_id=user.id,
        )

    monkeypatch.setattr("canyongis.web.server.submit_tool_job", fake_submit_tool_job)
    app = create_app(output_root=tmp_path / "out", postgres=live_postgres)
    client = TestClient(app)

    snap = client.post(
        "/api/jobs/kml-snap",
        json={"inputs": {"kml": "kml-1"}, "params": {"target": "dem"}},
    )
    profile = client.post(
        "/api/jobs/track-profile",
        json={
            "inputs": {"kml": "kml-1", "dem": "dem-1"},
            "params": {"category": "descent", "track_index": 0},
        },
    )
    spine = client.post(
        "/api/jobs/ravine-spine",
        json={"inputs": {"score": "tpi-1"}, "params": {"threshold_pct": 95}},
    )

    assert snap.status_code == 200, snap.text
    assert profile.status_code == 200, profile.text
    assert spine.status_code == 200, spine.text
    assert calls[0]["tool_key"] == "kml-snap"
    assert calls[0]["body"]["inputs"] == {"kml": "kml-1"}
    assert calls[1]["tool_key"] == "track-profile"
    assert calls[1]["body"]["inputs"] == {"kml": "kml-1", "dem": "dem-1"}
    assert calls[2]["tool_key"] == "ravine-spine"
    assert calls[2]["body"]["inputs"] == {"score": "tpi-1"}

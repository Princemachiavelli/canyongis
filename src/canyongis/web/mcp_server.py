from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict
from getpass import getuser
from pathlib import Path
from typing import Any

import geopandas as gpd
from fastmcp import FastMCP

from ..aoi_mosaic import build_aoi_dem_mosaic
from ..ingest import (
    ROPEWIKI_PAGE_URL,
    _download_file,
    _find_kml_file_title,
    _get_kml_download_url,
    parse_ropewiki_ref,
)
from ..kml_snap import snap_kml_to_raster
from ..ravine_spine import export_ravine_spines
from ..track_profile import _rappels_geojson, analyze_track_profile
from .auth import User
from .db import PostgresConfig
from .dem_jobs import list_specs
from .dispatcher import DispatchError, submit_tool_job
from .server import (
    WebContext,
    _build_kml_import_result,
    _compare_display_high,
    _compare_raster_path,
    _gdf_to_geojson,
    _job_response,
    _raster_bounds_wgs84,
    _ravine_spine_layers,
    _safe_kml_name,
    _safe_name,
    _validate_kml_container,
)

mcp = FastMCP("canyongis")
_ctx: WebContext | None = None


def ctx() -> WebContext:
    assert _ctx is not None
    return _ctx


def _resolve_user() -> dict[str, Any]:
    wc = ctx()
    if wc.auth.single_user:
        user = wc.auth.local_user()
        if user is None:
            raise RuntimeError("single-user mode but 'local' user is missing")
        return {"id": user.id, "username": user.username}
    username = os.environ.get("CANYONGIS_MCP_USERNAME")
    if not username:
        raise RuntimeError("multi-user mode requires CANYONGIS_MCP_USERNAME env var")
    user = wc.auth.get_user_by_username(username)
    if user is None:
        raise RuntimeError(f"user not found: {username}")
    return {"id": user[0].id, "username": user[0].username}


def _user_obj(info: dict[str, Any]) -> User:
    return User(id=info["id"], username=info["username"])


def _project_id(info: dict[str, Any]) -> str:
    pid = os.environ.get("CANYONGIS_MCP_PROJECT_ID")
    if pid:
        return pid
    return ctx().store.ensure_personal_project(_user_obj(info))


# ── Tools ──────────────────────────────────────────────────────────────


@mcp.tool(description="Check if the server is healthy and PostgreSQL is reachable.")
def health() -> dict[str, str]:
    return {"status": "ok"}


@mcp.tool(description="List projects accessible to the current authenticated user.")
def list_projects() -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    with wc.store.conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, m.role
            FROM canyongis_projects p
            JOIN canyongis_project_memberships m ON m.project_id = p.id
            WHERE m.user_id = %s
            ORDER BY p.created_at
            """,
            (user_info["id"],),
        )
        projects = [{"id": row[0], "name": row[1], "role": row[2]} for row in cur.fetchall()]
    return {"projects": projects}


@mcp.tool(description="List available analysis tools and registered DEM job specs.")
def list_tools() -> dict[str, Any]:
    builtins = [
        {"id": "build-dem", "label": "Build DEM mosaic"},
        {"id": "ravine-spine", "label": "Ravine spine from TPI"},
        {"id": "kml-snap", "label": "Snap KML to terrain"},
        {"id": "track-profile", "label": "Track elevation profile"},
    ]
    registered = [{"id": s.key, "label": s.label} for s in list_specs()]
    return {"tools": [*builtins, *registered]}


@mcp.tool(description="List all jobs. Optionally filter by project_id.")
def list_jobs(project_id: str | None = None) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = project_id or _project_id(user_info)
    with wc.lock:
        wc.store.reload()
        jobs = [_job_response(wc, job) for job in wc.store.jobs.values() if job.project_id == pid]
    return {"jobs": sorted(jobs, key=lambda row: row["created_at"], reverse=True)}


@mcp.tool(description="Get detailed status, result, and progress for a job by ID.")
def get_job(job_id: str) -> dict[str, Any]:
    return _job_response(ctx(), ctx().job_or_404(job_id))


@mcp.tool(description="Cancel a queued or running job by ID.")
def cancel_job(job_id: str) -> dict[str, Any]:
    user_info = _resolve_user()
    return _job_response(ctx(), ctx().cancel_job(job_id, user=_user_obj(user_info)))


@mcp.tool(
    description=(
        "Import a canyon route from Ropewiki by page URL or reference."
        " Returns a job ID immediately — poll with get_job for completion and results."
    )
)
def import_kml_ropewiki(source: str, container_dem_job_id: str | None = None) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    params = {"source": source, "container_dem_job_id": container_dem_job_id}

    def run(progress_cb, job_id: str) -> dict[str, Any]:
        ref = parse_ropewiki_ref(source)
        file_title, page_title = _find_kml_file_title(ref)
        if file_title is None:
            raise ValueError(f"Ropewiki page has no KML attachment: {source}")
        download_url = _get_kml_download_url(file_title)
        if download_url is None:
            raise ValueError(f"Ropewiki KML has no download URL: {file_title}")
        name = _safe_kml_name(file_title.removeprefix("File:"))
        kml_path = wc.output_root / "kml" / job_id / _safe_kml_name(name)
        kml_path.parent.mkdir(parents=True, exist_ok=True)
        progress_cb(f"download Ropewiki KML {file_title} -> {kml_path}")
        _download_file(download_url, kml_path)
        result = _build_kml_import_result(
            wc,
            kml_path,
            Path(name).stem,
            source={
                "type": "ropewiki",
                "source": source,
                "page_title": page_title,
                "page_url": (
                    ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_"))
                    if page_title
                    else source
                ),
                "file_title": file_title,
                "download_url": download_url,
            },
            container_dem_job_id=container_dem_job_id,
        )
        try:
            _validate_kml_container(wc, container_dem_job_id, result["bounds_wgs84"])
        except Exception:
            kml_path.unlink(missing_ok=True)
            raise
        return result

    job = wc.submit(
        "kml-import", params, run, user=_user_obj(user_info), project_id=pid, include_job_id=True
    )
    return asdict(job)


@mcp.tool(
    description=(
        "Build a DEM mosaic from USGS 1m tiles covering the given bounding box."
        " Returns a job ID immediately — poll with get_job for the DEM path and bounds."
    )
)
def build_dem(
    name: str = "aoi",
    west: float = 0,
    south: float = 0,
    east: float = 0,
    north: float = 0,
    buffer_m: float = 200.0,
    max_products: int = 24,
) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    safe_name = _safe_name(name)
    params = {
        "name": safe_name,
        "bounds": {"west": west, "south": south, "east": east, "north": north},
        "buffer_m": buffer_m,
        "max_products": max_products,
    }

    def run(progress_cb) -> dict[str, Any]:
        out_dir = wc.output_root / "areas" / safe_name
        dem_path = out_dir / "dem.tif"
        progress_cb(f"build-dem -> {dem_path} (buffer_m={buffer_m})")
        result = build_aoi_dem_mosaic(
            (west, south, east, north),
            dem_path,
            cache_dir=wc.dem_cache,
            buffer_m=buffer_m,
            max_products=max_products,
        )
        return {
            **asdict(result),
            "dem_url": wc.output_url(result.dem_path),
            "bounds_wgs84": result.bounds_wgs84,
        }

    job = wc.submit("build-dem", params, run, user=_user_obj(user_info), project_id=pid)
    return asdict(job)


@mcp.tool(
    description=(
        "Run any registered DEM-derived analysis tool. inputs_json is a JSON object; "
        "use {'dem': '<job_id>'} or omit it to use the latest completed DEM in the project. "
        "params_json must match the tool's request schema."
    )
)
def run_tool(tool_key: str, inputs_json: str = "{}", params_json: str = "{}") -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    try:
        inputs = json.loads(inputs_json)
        params = json.loads(params_json)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(inputs, dict):
        raise ValueError("inputs_json must decode to an object")
    if not isinstance(params, dict):
        raise ValueError("params_json must decode to an object")
    try:
        job = submit_tool_job(
            wc,
            tool_key,
            {"inputs": inputs, "params": params},
            user=_user_obj(user_info),
            project_id=pid,
        )
    except DispatchError as exc:
        raise ValueError(str(exc.detail)) from exc
    return asdict(job)


@mcp.tool(
    description=(
        "Run a DEM-derived analysis job on a completed DEM."
        " job_key options: hillshade, drainage, tpi-ravine, geomorphon-ravine, curvature, rvt."
        " params is a JSON string matching the job's request schema."
        " Use the job_specs tool to see available parameters for each job_key."
    )
)
def run_dem_job(dem_job_id: str, job_key: str, params: str = "{}") -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    try:
        params_body = json.loads(params)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(params_body, dict):
        raise ValueError("params must decode to an object")
    try:
        job = submit_tool_job(
            wc,
            job_key,
            {"inputs": {"dem": dem_job_id}, "params": params_body},
            user=_user_obj(user_info),
            project_id=pid,
        )
    except DispatchError as exc:
        raise ValueError(str(exc.detail)) from exc
    return asdict(job)


@mcp.tool(
    description=(
        "Snap a KML track to terrain (target='dem') or a score raster"
        " (target='tpi', 'geomorphon', 'curvature')."
        " Provide the kml_job_id from a successful KML import."
    )
)
def snap_kml(
    kml_job_id: str,
    dem_job_id: str | None = None,
    compare_job_id: str | None = None,
    target: str = "dem",
    search_radius_m: float = 30.0,
    sample_step_m: float = 10.0,
    high_threshold: float | None = None,
) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    kml_job = wc.job_or_404(kml_job_id)
    if kml_job.status != "succeeded" or not kml_job.result:
        raise ValueError("KML import job has not succeeded")

    kml_path = Path(str(kml_job.result["kml_path"]))
    dem_path: Path | None = None
    raster_path: Path
    snap_mode = "min" if target == "dem" else "max"
    hi_threshold = high_threshold

    if target == "dem":
        if not dem_job_id:
            raise ValueError("dem_job_id is required for DEM snapping")
        dem_j = wc.job_or_404(dem_job_id)
        if dem_j.status != "succeeded" or not dem_j.result:
            raise ValueError("DEM job has not succeeded")
        dem_path = Path(str(dem_j.result["dem_path"]))
        raster_path = dem_path
    else:
        if not compare_job_id:
            raise ValueError("compare_job_id is required for score-raster snapping")
        compare_job = wc.job_or_404(compare_job_id)
        if compare_job.status != "succeeded" or not compare_job.result:
            raise ValueError("compare job has not succeeded")
        dem_path = Path(str(compare_job.result["dem_path"]))
        raster_path = _compare_raster_path(compare_job, target)
        if hi_threshold is None:
            hi_threshold = _compare_display_high(compare_job, target)
    if not raster_path.exists():
        raise ValueError(f"snap raster does not exist: {raster_path}")

    params = {
        "kml_job_id": kml_job_id,
        "target": target,
        "search_radius_m": search_radius_m,
        "sample_step_m": sample_step_m,
    }

    def run(progress_cb) -> dict[str, Any]:
        out_dir = wc.output_root / "jobs" / f"kml-snap-{uuid.uuid4().hex[:8]}"
        kml_name = Path(str(kml_job.result.get("name") or kml_path.name)).stem
        name = f"{kml_name}-{target}-snapped.kml"
        progress_cb(
            f"kml-snap -> {out_dir} "
            f"(target={target}, radius={search_radius_m:g}m, "
            f"high_threshold={hi_threshold})"
        )
        result = snap_kml_to_raster(
            kml_path,
            raster_path,
            out_dir,
            snap_mode=snap_mode,
            search_radius_m=search_radius_m,
            sample_step_m=sample_step_m,
            high_threshold=hi_threshold,
            output_name=_safe_kml_name(name),
        )
        snapped_gdf = gpd.read_file(result.snapped_gpkg_path)
        geom_type = snapped_gdf.geometry.geom_type
        line_gdf = snapped_gdf[geom_type.isin(["LineString", "MultiLineString"])].reset_index(
            drop=True
        )
        point_gdf = snapped_gdf[geom_type.isin(["Point", "MultiPoint"])].reset_index(drop=True)
        category = line_gdf["category"] if "category" in line_gdf.columns else None
        descent = (
            line_gdf[category == "descent"].reset_index(drop=True)
            if category is not None
            else line_gdf.iloc[0:0]
        )
        approach = (
            line_gdf[category == "approach"].reset_index(drop=True)
            if category is not None
            else line_gdf.iloc[0:0]
        )
        unclassified = (
            line_gdf[~category.isin(["descent", "approach"])].reset_index(drop=True)
            if category is not None
            else line_gdf
        )
        bounds = _raster_bounds_wgs84(dem_path) if dem_path else _raster_bounds_wgs84(raster_path)
        return {
            **asdict(result),
            "name": Path(result.snapped_kml_path).name,
            "kml_path": result.snapped_kml_path,
            "kml_url": wc.output_url(result.snapped_kml_path),
            "bounds_wgs84": bounds,
            "descent_geojson": _gdf_to_geojson(descent),
            "approach_geojson": _gdf_to_geojson(approach),
            "unclassified_geojson": _gdf_to_geojson(unclassified),
            "points_geojson": _gdf_to_geojson(point_gdf),
            "source": {
                "kind": "kml-snap",
                "input_kml_job_id": kml_job_id,
                "target": target,
                "raster_path": str(raster_path),
            },
        }

    job = wc.submit("kml-snap", params, run, user=_user_obj(user_info), project_id=pid)
    return asdict(job)


@mcp.tool(
    description=(
        "Analyze elevation profile and detect rappel candidates from a KML track."
        " Requires kml_job_id and dem_job_id from successful jobs."
    )
)
def track_profile(
    kml_job_id: str,
    dem_job_id: str,
    category: str = "descent",
    track_index: int = 0,
    sample_step_m: float = 5.0,
    rappel_window_m: float = 10.0,
    min_rappel_drop_m: float = 3.0,
    min_rappel_slope: float = 0.45,
) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    kml_job = wc.job_or_404(kml_job_id)
    if kml_job.status != "succeeded" or not kml_job.result:
        raise ValueError("KML job has not succeeded")
    dem_job = wc.job_or_404(dem_job_id)
    if dem_job.status != "succeeded" or not dem_job.result:
        raise ValueError("DEM job has not succeeded")

    kml_path = Path(str(kml_job.result["kml_path"]))
    dem_path = Path(str(dem_job.result["dem_path"]))
    params = {
        "kml_job_id": kml_job_id,
        "dem_job_id": dem_job_id,
        "category": category,
        "track_index": track_index,
    }

    def run(progress_cb) -> dict[str, Any]:
        progress_cb(
            f"track-profile {category}[{track_index}] "
            f"step={sample_step_m:g}m window={rappel_window_m:g}m"
        )
        result = analyze_track_profile(
            kml_path,
            dem_path,
            category=category,
            track_index=track_index,
            sample_step_m=sample_step_m,
            rappel_window_m=rappel_window_m,
            min_rappel_drop_m=min_rappel_drop_m,
            min_rappel_slope=min_rappel_slope,
        )
        payload = asdict(result)
        payload["rappel_candidates_geojson"] = _rappels_geojson(result.rappel_candidates)
        payload["source"] = {
            "kind": "track-profile",
            "kml_job_id": kml_job_id,
            "dem_job_id": dem_job_id,
        }
        return payload

    job = wc.submit("track-profile", params, run, user=_user_obj(user_info), project_id=pid)
    return asdict(job)


@mcp.tool(
    description=(
        "Extract ravine spines from a TPI job result."
        " Provide the tpi_job_id from a successful tpi-ravine run."
    )
)
def ravine_spine(
    tpi_job_id: str,
    threshold_pct: float = 94.0,
    hysteresis_ratio: float = 0.75,
    branch_keep_ratio: float = 0.20,
    max_path_slope: float = 0.30,
    parent_kml_job_id: str | None = None,
) -> dict[str, Any]:
    user_info = _resolve_user()
    wc = ctx()
    pid = _project_id(user_info)
    tpi_job = wc.job_or_404(tpi_job_id)
    if tpi_job.status != "succeeded" or not tpi_job.result:
        raise ValueError("TPI job has not succeeded")
    if "b_tpi_path" not in tpi_job.result or "dem_path" not in tpi_job.result:
        raise ValueError("TPI job missing b_tpi_path/dem_path")

    if parent_kml_job_id:
        parent_kml_job = wc.job_or_404(parent_kml_job_id)
        if parent_kml_job.status != "succeeded" or not parent_kml_job.result:
            raise ValueError("parent KML job has not succeeded")

    params = {
        "tpi_job_id": tpi_job_id,
        "threshold_pct": threshold_pct,
        "hysteresis_ratio": hysteresis_ratio,
        "branch_keep_ratio": branch_keep_ratio,
        "max_path_slope": max_path_slope,
        "parent_kml_job_id": parent_kml_job_id,
    }

    def run(progress_cb) -> dict[str, Any]:
        out_dir = wc.output_root / "jobs" / f"ravine-spine-{uuid.uuid4().hex[:8]}"
        progress_cb(f"ravine-spine -> {out_dir}")
        result = export_ravine_spines(
            tpi_job.result["b_tpi_path"],
            tpi_job.result["dem_path"],
            out_dir,
            score_closing_m=5.0,
            auto_threshold="fixed",
            threshold_pct=threshold_pct,
            hysteresis_ratio=hysteresis_ratio,
            branch_keep_ratio=branch_keep_ratio,
            max_path_slope=max_path_slope,
        )
        return {
            **asdict(result),
            "bounds_wgs84": tpi_job.result.get("bounds_wgs84"),
            "parent_kml_job_id": parent_kml_job_id,
            **_ravine_spine_layers(wc, asdict(result)),
        }

    job = wc.submit("ravine-spine", params, run, user=_user_obj(user_info), project_id=pid)
    return asdict(job)


@mcp.tool(
    description=(
        "Get detailed job spec definitions including request schemas and"
        " available visualizations for DEM-derived analysis jobs."
    )
)
def job_specs() -> dict[str, Any]:
    import contextlib

    out: list[dict[str, Any]] = []
    for spec in list_specs():
        defaults = {}
        with contextlib.suppress(Exception):
            defaults = spec.request_model().model_dump()
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "description": spec.description,
                "visualizations": [{"key": v.key, "label": v.label} for v in spec.visualizations],
                "inputs": [asdict(input_spec) for input_spec in spec.inputs],
                "outputs": [asdict(output_spec) for output_spec in spec.outputs],
                "layers": [asdict(layer_spec) for layer_spec in spec.layers],
                "request_defaults": defaults,
                "available": spec.available,
                "unavailable_reason": spec.unavailable_reason,
            }
        )
    return {"specs": out}


# ── CLI ────────────────────────────────────────────────────────────────


def cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-mcp-server",
        description="FastMCP server exposing canyongis operations as MCP tools.",
    )
    p.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="MCP transport (default: stdio)",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for SSE transport (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=9876,
        help="Bind port for SSE transport (default: 9876)",
    )
    p.add_argument("--output-root", default="runs/web", help="Job/output root (default: runs/web).")
    p.add_argument("--pg-host", default=os.environ.get("CANYONGIS_PG_HOST", "localhost"))
    p.add_argument("--pg-port", type=int, default=int(os.environ.get("CANYONGIS_PG_PORT", "5433")))
    p.add_argument("--pg-db", default=os.environ.get("CANYONGIS_PG_DB", "postgres"))
    p.add_argument(
        "--pg-user",
        default=os.environ.get("CANYONGIS_PG_USER", os.environ.get("PGUSER", getuser())),
    )
    p.add_argument(
        "--pg-password",
        default=os.environ.get("CANYONGIS_PG_PASSWORD", os.environ.get("PGPASSWORD")),
    )
    p.add_argument("--pg-dsn", default=os.environ.get("CANYONGIS_PG_DSN"))
    args = p.parse_args(argv)

    postgres = PostgresConfig(
        host=args.pg_host,
        port=args.pg_port,
        dbname=args.pg_db,
        user=args.pg_user,
        password=args.pg_password,
        dsn=args.pg_dsn,
    )

    global _ctx
    _ctx = WebContext(Path(args.output_root), postgres=postgres)

    kwargs = {}
    if args.transport == "sse":
        kwargs["host"] = args.host
        kwargs["port"] = args.port
    mcp.run(transport=args.transport, **kwargs)


if __name__ == "__main__":
    cli()

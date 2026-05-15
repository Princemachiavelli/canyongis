from __future__ import annotations

import argparse
import json
import os
import shutil
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from getpass import getuser
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

import geopandas as gpd
import psycopg
import rasterio
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError
from rasterio.warp import transform_bounds

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
from ..validate import parse_kml
from .assets import _is_path_key, extract_asset_candidates, new_asset_id
from .auth import (
    AuthManager,
    User,
    build_auth_manager,
    clear_session_cookie,
    get_current_user,
    set_session_cookie,
)
from .db import PostgresConfig, apply_migrations
from .dem_jobs import get_spec, list_specs, normalize_result
from .dispatcher import DispatchError, submit_tool_job

JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
DEFAULT_PROJECT_NAME = "Personal"


PROGRESS_CAP = 200


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus
    created_at: str
    updated_at: str
    params: dict[str, Any]
    project_id: str | None = None
    created_by_user_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    progress: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    canceled_at: str | None = None
    canceled_by_user_id: str | None = None


@dataclass
class JobStore:
    output_root: Path
    pg: PostgresConfig
    jobs: dict[str, Job] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.conn = psycopg.connect(**self.pg.connect_kwargs())
        self.conn.autocommit = True
        self._migrate_jobs_json()
        self.reload()

    @property
    def path(self) -> Path:
        return self.output_root / "jobs.json"

    def _migrate_jobs_json(self) -> None:
        if not self.path.exists():
            return
        rows = json.loads(self.path.read_text())
        with self.conn.cursor() as cur:
            for row in rows:
                job = Job(**row)
                cur.execute(
                    """
                    INSERT INTO canyongis_jobs
                        (id, output_root, kind, status, created_at, updated_at,
                         params, result, error, progress)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        output_root = EXCLUDED.output_root
                    WHERE canyongis_jobs.output_root = ''
                    """,
                    (
                        job.id,
                        str(self.output_root.resolve()),
                        job.kind,
                        job.status,
                        job.created_at,
                        job.updated_at,
                        Jsonb(job.params),
                        Jsonb(job.result) if job.result is not None else None,
                        job.error,
                        Jsonb(job.progress),
                    ),
                )

    def reload(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, kind, status, created_at, updated_at, params, result, error,
                       progress, project_id, created_by_user_id,
                       started_at, finished_at, canceled_at, canceled_by_user_id
                FROM canyongis_jobs
                WHERE output_root = %s
                ORDER BY created_at
                """,
                (str(self.output_root.resolve()),),
            )
            self.jobs = {
                row[0]: Job(
                    id=row[0],
                    kind=row[1],
                    status=row[2],
                    created_at=row[3],
                    updated_at=row[4],
                    params=row[5] or {},
                    result=row[6],
                    error=row[7],
                    progress=row[8] or [],
                    project_id=row[9],
                    created_by_user_id=row[10],
                    started_at=row[11],
                    finished_at=row[12],
                    canceled_at=row[13],
                    canceled_by_user_id=row[14],
                )
                for row in cur.fetchall()
            }

    def upsert(self, job: Job) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO canyongis_jobs
                    (id, output_root, kind, status, created_at, updated_at,
                     params, result, error, progress,
                     project_id, created_by_user_id,
                     started_at, finished_at, canceled_at, canceled_by_user_id)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    output_root = EXCLUDED.output_root,
                    kind = EXCLUDED.kind,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    params = EXCLUDED.params,
                    result = EXCLUDED.result,
                    error = EXCLUDED.error,
                    progress = EXCLUDED.progress,
                    project_id = EXCLUDED.project_id,
                    created_by_user_id = EXCLUDED.created_by_user_id,
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    canceled_at = EXCLUDED.canceled_at,
                    canceled_by_user_id = EXCLUDED.canceled_by_user_id
                """,
                (
                    job.id,
                    str(self.output_root.resolve()),
                    job.kind,
                    job.status,
                    job.created_at,
                    job.updated_at,
                    Jsonb(job.params),
                    Jsonb(job.result) if job.result is not None else None,
                    job.error,
                    Jsonb(job.progress),
                    job.project_id,
                    job.created_by_user_id,
                    job.started_at,
                    job.finished_at,
                    job.canceled_at,
                    job.canceled_by_user_id,
                ),
            )
        self.jobs[job.id] = job

    def delete(self, job_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM canyongis_jobs WHERE id = %s AND output_root = %s",
                (job_id, str(self.output_root.resolve())),
            )
        self.jobs.pop(job_id, None)

    def save(self) -> None:
        for job in self.jobs.values():
            self.upsert(job)

    def register_assets(
        self, job: Job, *, project_id: str, created_by_user_id: str | None
    ) -> list[str]:
        """Insert asset rows for files newly produced by ``job``. Returns new asset ids."""
        candidates = extract_asset_candidates(job.result, output_root=self.output_root)
        if not candidates:
            return []
        new_ids: list[str] = []
        with self.conn.cursor() as cur:
            for cand in candidates:
                cur.execute(
                    "SELECT 1 FROM canyongis_assets WHERE project_id = %s AND path = %s LIMIT 1",
                    (project_id, cand.path),
                )
                if cur.fetchone() is not None:
                    continue
                asset_id = new_asset_id()
                try:
                    size_bytes = Path(cand.path).stat().st_size
                except OSError:
                    size_bytes = None
                bounds = cand.metadata.get("bounds_wgs84")
                cur.execute(
                    """
                    INSERT INTO canyongis_assets
                        (id, project_id, job_id, created_by_user_id, kind, path, url,
                         bounds_wgs84, format, media_type, size_bytes, metadata, created_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        asset_id,
                        project_id,
                        job.id,
                        created_by_user_id,
                        cand.kind,
                        cand.path,
                        cand.url,
                        Jsonb(bounds) if bounds is not None else None,
                        cand.format,
                        cand.media_type,
                        size_bytes,
                        Jsonb(cand.metadata),
                        job.finished_at or job.updated_at,
                    ),
                )
                new_ids.append(asset_id)
        return new_ids

    def ensure_personal_project(self, user: User) -> str:
        """Return the project_id of ``user``'s Personal project, creating if absent."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id FROM canyongis_projects p
                JOIN canyongis_project_memberships m ON m.project_id = p.id
                WHERE m.user_id = %s AND p.name = %s
                ORDER BY p.created_at LIMIT 1
                """,
                (user.id, DEFAULT_PROJECT_NAME),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
            project_id = uuid.uuid4().hex[:16]
            cur.execute(
                "INSERT INTO canyongis_projects (id, name, created_by_user_id) VALUES (%s, %s, %s)",
                (project_id, DEFAULT_PROJECT_NAME, user.id),
            )
            cur.execute(
                "INSERT INTO canyongis_project_memberships (project_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (project_id, user.id),
            )
        return project_id


class Bounds(BaseModel):
    west: float
    south: float
    east: float
    north: float

    def tuple(self) -> tuple[float, float, float, float]:  # type: ignore[override]
        if self.west >= self.east:
            raise ValueError("west must be < east")
        if self.south >= self.north:
            raise ValueError("south must be < north")
        return (self.west, self.south, self.east, self.north)


class BuildDemRequest(BaseModel):
    name: str = Field(default="aoi", min_length=1)
    bounds: Bounds
    buffer_m: float = 200.0
    max_products: int = 24


class RavineSpineRequest(BaseModel):
    threshold_pct: float = 94.0
    hysteresis_ratio: float = 0.75
    branch_keep_ratio: float = 0.20
    max_path_slope: float = 0.30
    parent_kml_job_id: str | None = None


class RopewikiImportRequest(BaseModel):
    source: str = Field(..., min_length=1)
    container_dem_job_id: str | None = None


class KmlSnapRequest(BaseModel):
    dem_job_id: str | None = None
    compare_job_id: str | None = None
    target: Literal["dem", "tpi", "geomorphon", "curvature"] = "dem"
    search_radius_m: float = Field(default=30.0, gt=0)
    sample_step_m: float = Field(default=10.0, gt=0)
    high_threshold: float | None = None


class TrackProfileRequest(BaseModel):
    dem_job_id: str
    category: Literal["descent", "approach", "unclassified"] = "descent"
    track_index: int = Field(default=0, ge=0)
    sample_step_m: float = Field(default=5.0, gt=0)
    rappel_window_m: float = Field(default=10.0, gt=0)
    min_rappel_drop_m: float = Field(default=3.0, gt=0)
    min_rappel_slope: float = Field(default=0.45, gt=0)


class WebContext:
    def __init__(
        self,
        output_root: Path,
        frontend_dist: Path | None = None,
        postgres: PostgresConfig | None = None,
    ) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.dem_cache = self.output_root / "dem-cache"
        self.pg = postgres or PostgresConfig()
        apply_migrations(self.pg)
        self.store = JobStore(self.output_root, self.pg)
        self.auth = build_auth_manager(self.pg, self.output_root)
        self.lock = Lock()
        self.futures: dict[str, Future] = {}
        self.executor = ThreadPoolExecutor(
            max_workers=int(os.environ.get("CANYONGIS_WEB_WORKERS", "4"))
        )
        self.frontend_dist = frontend_dist
        self._recover_incomplete_jobs()

    def _recover_incomplete_jobs(self) -> None:
        with self.lock:
            changed = False
            for job in self.store.jobs.values():
                if job.status in {"queued", "running"}:
                    job.status = "canceled"
                    job.updated_at = self.now()
                    job.error = (
                        "Job was queued or running when the server stopped; "
                        "marked canceled on startup."
                    )
                    changed = True
            if changed:
                self.store.save()

    def now(self) -> str:
        return datetime.now(UTC).isoformat()

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        fn,
        *,
        user: User,
        project_id: str,
        include_job_id: bool = False,
    ) -> Job:
        """Submit a job. fn is called as fn(progress_cb) on a worker thread.
        progress_cb appends a line to the job's progress log."""
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            status="queued",
            created_at=self.now(),
            updated_at=self.now(),
            params=params,
            project_id=project_id,
            created_by_user_id=user.id,
        )
        with self.lock:
            self.store.upsert(job)
        future = self.executor.submit(self._run, job.id, fn, include_job_id)
        with self.lock:
            self.futures[job.id] = future
        return job

    def _run(self, job_id: str, fn, include_job_id: bool) -> None:
        if self.is_canceled(job_id):
            return
        self._update(job_id, status="running", started_at=self.now())

        def progress_cb(line: str) -> None:
            if self.is_canceled(job_id):
                raise RuntimeError("job canceled")
            self.append_progress(job_id, line)

        try:
            result = fn(progress_cb, job_id) if include_job_id else fn(progress_cb)
        except Exception as exc:  # pragma: no cover - exercised by real jobs
            if self.is_canceled(job_id):
                self._update(
                    job_id, status="canceled", error="Job canceled.", finished_at=self.now()
                )
            else:
                self._update(
                    job_id,
                    status="failed",
                    error=f"{exc}\n{traceback.format_exc()}",
                    finished_at=self.now(),
                )
            with self.lock:
                self.futures.pop(job_id, None)
            return
        if self.is_canceled(job_id):
            self._update(job_id, status="canceled", error="Job canceled.", finished_at=self.now())
        else:
            self._update(job_id, status="succeeded", result=result, finished_at=self.now())
            self._register_assets(job_id)
        with self.lock:
            self.futures.pop(job_id, None)

    def _register_assets(self, job_id: str) -> None:
        with self.lock:
            job = self.store.jobs.get(job_id)
            if job is None or job.project_id is None:
                return
            try:
                self.store.register_assets(
                    job,
                    project_id=job.project_id,
                    created_by_user_id=job.created_by_user_id,
                )
            except Exception:
                traceback.print_exc()

    def _update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        canceled_at: str | None = None,
        canceled_by_user_id: str | None = None,
    ) -> None:
        with self.lock:
            job = self.store.jobs[job_id]
            if job.status == "canceled" and status != "canceled":
                return
            job.status = status
            job.updated_at = self.now()
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            if started_at is not None:
                job.started_at = started_at
            if finished_at is not None:
                job.finished_at = finished_at
            if canceled_at is not None:
                job.canceled_at = canceled_at
            if canceled_by_user_id is not None:
                job.canceled_by_user_id = canceled_by_user_id
            self.store.upsert(job)

    def is_canceled(self, job_id: str) -> bool:
        with self.lock:
            job = self.store.jobs.get(job_id)
            return job is None or job.status == "canceled"

    def cancel_job(self, job_id: str, *, user: User | None = None) -> Job:
        with self.lock:
            job = self.store.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
            if job.status not in {"queued", "running"}:
                return job
            future = self.futures.get(job_id)
            if future is not None:
                future.cancel()
            now = self.now()
            job.status = "canceled"
            job.updated_at = now
            job.canceled_at = now
            job.finished_at = now
            if user is not None:
                job.canceled_by_user_id = user.id
            job.error = "Job canceled by user."
            self.store.upsert(job)
            return job

    def append_progress(self, job_id: str, line: str) -> None:
        with self.lock:
            job = self.store.jobs.get(job_id)
            if job is None:
                return
            job.progress.append(line)
            if len(job.progress) > PROGRESS_CAP:
                job.progress = job.progress[-PROGRESS_CAP:]
            job.updated_at = self.now()
            self.store.upsert(job)

    def job_or_404(self, job_id: str) -> Job:
        with self.lock:
            job = self.store.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return job

    def output_url(self, path: str | Path) -> str:
        rel = Path(path).resolve().relative_to(self.output_root.resolve())
        return f"/outputs/{rel.as_posix()}"


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")
    return safe or "aoi"


def _safe_kml_name(value: str) -> str:
    name = _safe_name(Path(value).name)
    if not name.lower().endswith(".kml"):
        name = f"{Path(name).stem}.kml"
    return name


def _raster_bounds_wgs84(path: str | Path) -> tuple[float, float, float, float]:
    with rasterio.open(path) as src:
        bounds = src.bounds
        return tuple(
            float(v)
            for v in transform_bounds(
                src.crs, "EPSG:4326", bounds.left, bounds.bottom, bounds.right, bounds.top
            )
        )


def _gdf_to_geojson(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(gdf.to_json())


def _build_kml_import_result(
    ctx: WebContext,
    kml_path: Path,
    name: str,
    *,
    source: dict[str, Any],
    container_dem_job_id: str | None = None,
) -> dict[str, Any]:
    features = parse_kml(kml_path)
    all_geoms = []
    for gdf in (
        features.descent_lines,
        features.approach_lines,
        features.unclassified_lines,
        features.points,
    ):
        if not gdf.empty:
            all_geoms.extend(gdf.geometry.tolist())
    if not all_geoms:
        raise ValueError("KML contains no geometry (no LineStrings or Points)")
    combined = gpd.GeoSeries(all_geoms, crs="EPSG:4326")
    west, south, east, north = (float(v) for v in combined.total_bounds)
    result = {
        "name": name,
        "kml_path": str(kml_path),
        "kml_url": ctx.output_url(kml_path),
        "out_dir": str(kml_path.parent),
        "bounds_wgs84": [west, south, east, north],
        "descent_geojson": _gdf_to_geojson(features.descent_lines),
        "approach_geojson": _gdf_to_geojson(features.approach_lines),
        "unclassified_geojson": _gdf_to_geojson(features.unclassified_lines),
        "points_geojson": _gdf_to_geojson(features.points),
        "counts": {
            "descent_lines": len(features.descent_lines),
            "approach_lines": len(features.approach_lines),
            "unclassified_lines": len(features.unclassified_lines),
            "points": len(features.points),
        },
        "source": source,
    }
    if container_dem_job_id:
        result["container_dem_job_id"] = container_dem_job_id
    return result


def _bounds_contains(
    outer: tuple[float, float, float, float] | list[float],
    inner: tuple[float, float, float, float] | list[float],
    *,
    epsilon: float = 1e-9,
) -> bool:
    ow, os, oe, on = (float(v) for v in outer)
    iw, is_, ie, in_ = (float(v) for v in inner)
    return iw >= ow - epsilon and is_ >= os - epsilon and ie <= oe + epsilon and in_ <= on + epsilon


def _validate_kml_container(
    ctx: WebContext, container_dem_job_id: str | None, bounds: list[float]
) -> None:
    if not container_dem_job_id:
        return
    dem_job = ctx.job_or_404(container_dem_job_id)
    if dem_job.status != "succeeded" or not dem_job.result:
        raise ValueError("target DEM container has not succeeded")
    dem_bounds = dem_job.result.get("bounds_wgs84")
    if not isinstance(dem_bounds, list) or len(dem_bounds) != 4:
        raise ValueError("target DEM container is missing bounds")
    if not _bounds_contains(dem_bounds, bounds):
        raise ValueError(
            "KML bounds are outside the selected DEM container; choose a different container "
            "or build a DEM for this KML first"
        )


def _parsed_kml_layer_result(kml_path: Path) -> dict[str, Any]:
    features = parse_kml(kml_path)
    all_geoms = []
    for gdf in (
        features.descent_lines,
        features.approach_lines,
        features.unclassified_lines,
        features.points,
    ):
        if not gdf.empty:
            all_geoms.extend(gdf.geometry.tolist())
    bounds = None
    if all_geoms:
        combined = gpd.GeoSeries(all_geoms, crs="EPSG:4326")
        bounds = [float(v) for v in combined.total_bounds]
    return {
        "bounds_wgs84": bounds,
        "descent_geojson": _gdf_to_geojson(features.descent_lines),
        "approach_geojson": _gdf_to_geojson(features.approach_lines),
        "unclassified_geojson": _gdf_to_geojson(features.unclassified_lines),
        "points_geojson": _gdf_to_geojson(features.points),
    }


def _ravine_spine_layers(ctx: WebContext, result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out_dir = Path(str(result.get("out_dir", ""))) if result.get("out_dir") else None
    spines_value = result.get("spines_path") or (out_dir / "spines.kml" if out_dir else None)
    rappels_value = result.get("rappel_candidates_path") or (
        out_dir / "rappel_candidates.kml" if out_dir else None
    )
    spines_path = Path(str(spines_value)) if spines_value else None
    rappels_path = Path(str(rappels_value)) if rappels_value else None
    if spines_path and spines_path.exists():
        out["spines_kml_url"] = ctx.output_url(spines_path)
        out["spines_layer"] = {
            "name": "Ravine spines",
            "kml_path": str(spines_path),
            "kml_url": ctx.output_url(spines_path),
            **_parsed_kml_layer_result(spines_path),
        }
    if rappels_path and rappels_path.exists():
        out["rappel_candidates_kml_url"] = ctx.output_url(rappels_path)
        out["rappel_candidates_layer"] = {
            "name": "Rappel candidates",
            "kml_path": str(rappels_path),
            "kml_url": ctx.output_url(rappels_path),
            **_parsed_kml_layer_result(rappels_path),
        }
    return out


def _ravine_spine_layer_path(result: dict[str, Any], layer_key: str) -> Path | None:
    out_dir = Path(str(result.get("out_dir", ""))) if result.get("out_dir") else None
    if layer_key == "spines_layer":
        value = result.get("spines_path") or (out_dir / "spines.kml" if out_dir else None)
    elif layer_key == "rappel_candidates_layer":
        value = result.get("rappel_candidates_path") or (
            out_dir / "rappel_candidates.kml" if out_dir else None
        )
    else:
        return None
    return Path(str(value)) if value else None


def _job_response(ctx: WebContext, job: Job) -> dict[str, Any]:
    row = asdict(job)
    spec = get_spec(job.kind)
    if spec is not None and job.status == "succeeded" and isinstance(row.get("result"), dict):
        row["result"] = normalize_result(spec, row["result"])
    if job.kind == "ravine-spine" and job.status == "succeeded" and row.get("result"):
        try:
            row["result"] = {**row["result"], **_ravine_spine_layers(ctx, row["result"])}
        except Exception:
            row["result"] = row["result"]
    return row


def _kml_import_path(ctx: WebContext, job_id: str, name: str) -> Path:
    return ctx.output_root / "kml" / job_id / _safe_kml_name(name)


def _compare_raster_path(compare_job: Job, target: str) -> Path:
    if not compare_job.result:
        raise HTTPException(status_code=400, detail="ravine-compare job has no result")
    if target == "tpi":
        return Path(str(compare_job.result["b_tpi_path"]))
    direct_field = {"geomorphon": "a_geomorphon_path", "curvature": "c_curvature_path"}.get(target)
    if direct_field and compare_job.result.get(direct_field):
        return Path(str(compare_job.result[direct_field]))
    out_dir = Path(str(compare_job.result.get("out_dir", "")))
    if not out_dir:
        raise HTTPException(
            status_code=400, detail="ravine-compare job is missing output directory"
        )
    if target == "geomorphon":
        return out_dir / "a_geomorphon.tif"
    if target == "curvature":
        return out_dir / "c_curvature.tif"
    raise HTTPException(status_code=400, detail=f"unsupported snap target: {target}")


def _compare_display_high(compare_job: Job, target: str) -> float | None:
    if not compare_job.result:
        return None
    out_dir = Path(str(compare_job.result.get("out_dir", "")))
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        return None
    try:
        summary = json.loads(summary_path.read_text())
    except Exception:
        return None
    nested_key = {"tpi": "b_tpi", "geomorphon": "a_geomorphon", "curvature": "c_curvature"}.get(
        target
    )
    if nested_key and isinstance(summary.get(nested_key), dict):
        value = summary[nested_key].get("display_hi")
        if isinstance(value, int | float):
            return float(value)
    value = summary.get("display_hi")
    return float(value) if isinstance(value, int | float) else None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _collect_output_paths(result: dict[str, Any] | None, *, output_root: Path) -> list[str]:
    """Return file paths the job actually produced — anything under ``out_dir``.

    Every job result has an ``out_dir`` key identifying the directory the job
    wrote to.  Input references (e.g. ``dem_path`` pointing to an upstream
    build-dem result) fall outside that directory and are excluded automatically.
    """
    if not result:
        return []
    root = output_root.resolve()
    out_dir_val = result.get("out_dir")
    if not isinstance(out_dir_val, str) or not out_dir_val:
        return []
    out_dir = Path(out_dir_val).resolve()
    try:
        out_dir.relative_to(root)
    except ValueError:
        return []

    seen: set[str] = set()
    paths: list[str] = []
    for key, value in result.items():
        if not isinstance(value, str) or not value:
            continue
        if not (_is_path_key(key) or key == "out_dir"):
            continue
        path = Path(value)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(out_dir)
        except ValueError:
            continue
        key_str = str(resolved)
        if key_str not in seen:
            seen.add(key_str)
            paths.append(key_str)
    return paths


def _delete_job_outputs(ctx: WebContext, job: Job) -> list[str]:
    """Delete files and directories this job produced during execution."""
    if not job.result:
        return []
    root = ctx.output_root.resolve()
    deleted: list[str] = []

    # Files the job wrote (all under out_dir).
    for path_str in _collect_output_paths(job.result, output_root=root):
        p = Path(path_str)
        if not _path_is_within(p, root):
            continue
        if p.is_dir():
            shutil.rmtree(p)
            deleted.append(path_str)
        elif p.exists():
            p.unlink()
            deleted.append(path_str)

    # The output directory itself.
    out_dir_val = job.result.get("out_dir")
    if isinstance(out_dir_val, str) and out_dir_val:
        out_dir = Path(out_dir_val).resolve()
        if out_dir.exists() and out_dir.is_dir() and _path_is_within(out_dir, root):
            try:
                shutil.rmtree(out_dir)
                deleted.append(str(out_dir))
            except OSError:
                pass

    return sorted(set(deleted))


def _current_user(request: Request) -> User:
    return get_current_user(request, request.app.state.auth)


CurrentUserDep = Depends(_current_user)


def _active_project_id(request: Request, user: User = CurrentUserDep) -> str:
    """Return the project_id from X-Project-Id header, or the user's Personal project."""
    ctx: WebContext = request.app.state.ctx
    project_id = request.headers.get("X-Project-Id")
    if project_id:
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM canyongis_project_memberships WHERE project_id = %s AND user_id = %s",
                (project_id, user.id),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=403,
                    detail=f"User is not a member of project {project_id}",
                )
        return project_id
    return ctx.store.ensure_personal_project(user)


ActiveProjectDep = Depends(_active_project_id)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def create_app(
    *,
    output_root: str | Path = "runs/web",
    frontend_dist: str | Path | None = None,
    postgres: PostgresConfig | None = None,
) -> FastAPI:
    if frontend_dist is None:
        frontend_dist = os.environ.get("CANYONGIS_FRONTEND_DIST", "web/frontend/dist")
    ctx = WebContext(
        Path(output_root),
        Path(frontend_dist) if frontend_dist is not None else None,
        postgres,
    )
    app = FastAPI(title="canyongis web", version="0.1.0")
    app.state.ctx = ctx
    app.state.auth = ctx.auth
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/outputs", StaticFiles(directory=ctx.output_root), name="outputs")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        auth: AuthManager = request.app.state.auth
        if auth.single_user:
            user = auth.local_user()
        else:
            token = request.cookies.get("canyongis_session")
            user_id = auth.load_session(token)
            user = auth.get_user_by_id(user_id) if user_id else None
        return {
            "authenticated": user is not None,
            "single_user": auth.single_user,
            "user": {"id": user.id, "username": user.username} if user else None,
        }

    @app.post("/api/auth/login")
    def auth_login(req: LoginRequest, response: Response) -> dict[str, Any]:
        user = ctx.auth.authenticate(req.username, req.password)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        set_session_cookie(response, ctx.auth.sign_session(user.id))
        return {"user": {"id": user.id, "username": user.username}}

    @app.post("/api/auth/logout")
    def auth_logout(response: Response) -> dict[str, bool]:
        clear_session_cookie(response)
        return {"ok": True}

    @app.get("/api/projects")
    def list_projects(user: User = CurrentUserDep) -> dict[str, Any]:
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.name, m.role
                FROM canyongis_projects p
                JOIN canyongis_project_memberships m ON m.project_id = p.id
                WHERE m.user_id = %s
                ORDER BY p.created_at
                """,
                (user.id,),
            )
            projects = [{"id": row[0], "name": row[1], "role": row[2]} for row in cur.fetchall()]
        return {"projects": projects}

    @app.get("/api/tools")
    def tools(user: User = CurrentUserDep) -> dict[str, Any]:
        builtins = [
            {"id": "build-dem", "label": "Build DEM mosaic"},
            {"id": "ravine-spine", "label": "Ravine spine from TPI"},
            {"id": "kml-snap", "label": "Snap KML to terrain"},
            {"id": "track-profile", "label": "Track elevation profile"},
        ]
        registered = [{"id": s.key, "label": s.label} for s in list_specs()]
        return {"tools": [*builtins, *registered]}

    @app.get("/api/job-specs")
    def job_specs(user: User = CurrentUserDep) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for spec in list_specs():
            try:
                defaults = spec.request_model().model_dump()
            except ValidationError:
                defaults = {}
            out.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "description": spec.description,
                    "visualizations": [
                        {"key": v.key, "label": v.label, "url_field": v.url_field}
                        for v in spec.visualizations
                    ],
                    "inputs": [asdict(input_spec) for input_spec in spec.inputs],
                    "outputs": [asdict(output_spec) for output_spec in spec.outputs],
                    "layers": [asdict(layer_spec) for layer_spec in spec.layers],
                    "request_defaults": defaults,
                    "request_schema": spec.request_model.model_json_schema(),
                    "available": spec.available,
                    "unavailable_reason": spec.unavailable_reason,
                }
            )
        return {"specs": out}

    @app.get("/api/jobs")
    def list_jobs(project_id: str = ActiveProjectDep) -> dict[str, Any]:
        with ctx.lock:
            ctx.store.reload()
            jobs = [
                _job_response(ctx, job)
                for job in ctx.store.jobs.values()
                if job.project_id == project_id
            ]
        return {"jobs": sorted(jobs, key=lambda row: row["created_at"], reverse=True)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        return _job_response(ctx, ctx.job_or_404(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        return _job_response(ctx, ctx.cancel_job(job_id, user=user))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        with ctx.lock:
            job = ctx.store.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
            if job.status in {"queued", "running"}:
                raise HTTPException(
                    status_code=400, detail="cancel running jobs before deleting them"
                )
            deleted_outputs = _delete_job_outputs(ctx, job)
            ctx.store.delete(job_id)
        return {"deleted": True, "job_id": job_id, "deleted_outputs": deleted_outputs}

    @app.delete("/api/jobs/{job_id}/kml-layer/{layer_key}")
    def delete_generated_kml_layer(
        job_id: str, layer_key: str, user: User = CurrentUserDep
    ) -> dict[str, Any]:
        with ctx.lock:
            job = ctx.store.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
            if job.status in {"queued", "running"}:
                raise HTTPException(status_code=400, detail="running jobs cannot be modified")
            if job.kind != "ravine-spine" or not job.result:
                raise HTTPException(status_code=400, detail="job has no generated KML layers")
            path = _ravine_spine_layer_path(job.result, layer_key)
            if path is None:
                raise HTTPException(status_code=404, detail=f"KML layer not found: {layer_key}")
            root = ctx.output_root.resolve()
            deleted_outputs: list[str] = []
            if path.exists():
                if not _path_is_within(path, root):
                    raise HTTPException(
                        status_code=400, detail="refusing to delete outside output root"
                    )
                path.unlink()
                deleted_outputs.append(str(path))
            for key in (
                layer_key,
                "spines_kml_url" if layer_key == "spines_layer" else "rappel_candidates_kml_url",
            ):
                job.result.pop(key, None)
            ctx.store.upsert(job)
        return {
            "deleted": True,
            "job_id": job_id,
            "layer_key": layer_key,
            "deleted_outputs": deleted_outputs,
        }

    @app.post("/api/jobs/import-kml-file")
    async def import_kml_file(
        file: UploadFile = File(...),  # noqa: B008
        container_dem_job_id: str | None = Form(default=None),
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        original_name = file.filename or "uploaded.kml"
        if not original_name.lower().endswith(".kml"):
            raise HTTPException(status_code=400, detail="uploaded file must have a .kml extension")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="uploaded KML file is empty")
        name = _safe_kml_name(original_name)
        params = {
            "name": name,
            "filename": original_name,
            "container_dem_job_id": container_dem_job_id,
        }

        def run(progress_cb, job_id: str) -> dict[str, Any]:
            kml_path = _kml_import_path(ctx, job_id, name)
            kml_path.parent.mkdir(parents=True, exist_ok=True)
            progress_cb(f"import-kml-file -> {kml_path}")
            kml_path.write_bytes(content)
            result = _build_kml_import_result(
                ctx,
                kml_path,
                Path(name).stem,
                source={"type": "file", "filename": original_name},
                container_dem_job_id=container_dem_job_id,
            )
            try:
                _validate_kml_container(
                    ctx,
                    container_dem_job_id,
                    result["bounds_wgs84"],
                )
            except Exception:
                kml_path.unlink(missing_ok=True)
                raise
            return result

        job = ctx.submit(
            "kml-import", params, run, user=user, project_id=project_id, include_job_id=True
        )
        return asdict(job)

    @app.post("/api/jobs/import-kml-ropewiki")
    def import_kml_ropewiki(
        req: RopewikiImportRequest, user: User = CurrentUserDep, project_id: str = ActiveProjectDep
    ) -> dict[str, Any]:
        source = req.source.strip()
        if not source:
            raise HTTPException(status_code=400, detail="source is required")
        params = {"source": source, "container_dem_job_id": req.container_dem_job_id}

        def run(progress_cb, job_id: str) -> dict[str, Any]:
            ref = parse_ropewiki_ref(source)
            file_title, page_title = _find_kml_file_title(ref)
            if file_title is None:
                raise ValueError(f"Ropewiki page has no KML attachment: {source}")
            download_url = _get_kml_download_url(file_title)
            if download_url is None:
                raise ValueError(f"Ropewiki KML has no download URL: {file_title}")

            name = _safe_kml_name(file_title.removeprefix("File:"))
            kml_path = _kml_import_path(ctx, job_id, name)
            kml_path.parent.mkdir(parents=True, exist_ok=True)
            progress_cb(f"download Ropewiki KML {file_title} -> {kml_path}")
            _download_file(download_url, kml_path)
            page_url = (
                ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_"))
                if page_title
                else source
            )
            result = _build_kml_import_result(
                ctx,
                kml_path,
                Path(name).stem,
                source={
                    "type": "ropewiki",
                    "source": source,
                    "page_title": page_title,
                    "page_url": page_url,
                    "file_title": file_title,
                    "download_url": download_url,
                },
                container_dem_job_id=req.container_dem_job_id,
            )
            try:
                _validate_kml_container(ctx, req.container_dem_job_id, result["bounds_wgs84"])
            except Exception:
                kml_path.unlink(missing_ok=True)
                raise
            return result

        job = ctx.submit(
            "kml-import", params, run, user=user, project_id=project_id, include_job_id=True
        )
        return asdict(job)

    @app.post("/api/jobs/build-dem")
    def build_dem(
        req: BuildDemRequest, user: User = CurrentUserDep, project_id: str = ActiveProjectDep
    ) -> dict[str, Any]:
        name = _safe_name(req.name)
        params = req.model_dump()

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "areas" / name
            dem_path = out_dir / "dem.tif"
            progress_cb(f"build-dem → {dem_path} (buffer_m={req.buffer_m})")
            result = build_aoi_dem_mosaic(
                req.bounds.tuple(),
                dem_path,
                cache_dir=ctx.dem_cache,
                buffer_m=req.buffer_m,
                max_products=req.max_products,
            )
            return {
                **asdict(result),
                "dem_url": ctx.output_url(result.dem_path),
                "bounds_wgs84": result.bounds_wgs84,
            }

        job = ctx.submit("build-dem", params, run, user=user, project_id=project_id)
        return asdict(job)

    @app.post("/api/jobs/{dem_job_id}/run/{job_key}")
    def run_dem_job(
        dem_job_id: str,
        job_key: str,
        body: Annotated[dict[str, Any] | None, Body()] = None,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        try:
            job = submit_tool_job(
                ctx,
                job_key,
                body,
                user=user,
                project_id=project_id,
                legacy_dem_job_id=dem_job_id,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return asdict(job)

    @app.post("/api/jobs/{tool_key}")
    def run_tool_job(
        tool_key: str,
        body: Annotated[dict[str, Any] | None, Body()] = None,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        try:
            job = submit_tool_job(ctx, tool_key, body, user=user, project_id=project_id)
        except DispatchError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return asdict(job)

    @app.post("/api/jobs/{tpi_job_id}/ravine-spine")
    def ravine_spine(
        tpi_job_id: str,
        req: RavineSpineRequest,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        tpi_job = ctx.job_or_404(tpi_job_id)
        if tpi_job.status != "succeeded" or not tpi_job.result:
            raise HTTPException(status_code=400, detail="upstream TPI job has not succeeded")
        if "b_tpi_path" not in tpi_job.result or "dem_path" not in tpi_job.result:
            raise HTTPException(
                status_code=400,
                detail="upstream job has no b_tpi_path / dem_path; run a TPI job first",
            )
        if req.parent_kml_job_id:
            parent_kml_job = ctx.job_or_404(req.parent_kml_job_id)
            if parent_kml_job.status != "succeeded" or not parent_kml_job.result:
                raise HTTPException(status_code=400, detail="parent KML job has not succeeded")
        params = {"tpi_job_id": tpi_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "jobs" / f"ravine-spine-{uuid.uuid4().hex[:8]}"
            progress_cb(f"ravine-spine → {out_dir}")
            result = export_ravine_spines(
                tpi_job.result["b_tpi_path"],
                tpi_job.result["dem_path"],
                out_dir,
                score_closing_m=5.0,
                auto_threshold="fixed",
                threshold_pct=req.threshold_pct,
                hysteresis_ratio=req.hysteresis_ratio,
                branch_keep_ratio=req.branch_keep_ratio,
                max_path_slope=req.max_path_slope,
            )
            return {
                **asdict(result),
                "bounds_wgs84": tpi_job.result.get("bounds_wgs84"),
                "parent_kml_job_id": req.parent_kml_job_id,
                **_ravine_spine_layers(ctx, asdict(result)),
            }

        job = ctx.submit("ravine-spine", params, run, user=user, project_id=project_id)
        return asdict(job)

    @app.post("/api/jobs/{dem_job_id}/trim-runs/{job_key}")
    def trim_job_runs(
        dem_job_id: str,
        job_key: str,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        with ctx.lock:
            ctx.store.reload()
            candidates = [
                j
                for j in ctx.store.jobs.values()
                if j.project_id == project_id
                and j.kind == job_key
                and (
                    j.params.get("dem_job_id") == dem_job_id
                    or (j.result and j.result.get("dem_job_id") == dem_job_id)
                )
            ]
            candidates.sort(key=lambda j: j.created_at, reverse=True)
            if len(candidates) <= 1:
                return {"deleted": 0, "message": "nothing to trim"}
            to_delete = candidates[1:]
            deleted_outputs: list[str] = []
            deleted_ids: list[str] = []
            for job in to_delete:
                if job.status in {"queued", "running"}:
                    continue
                deleted_outputs.extend(_delete_job_outputs(ctx, job))
                ctx.store.delete(job.id)
                deleted_ids.append(job.id)
            return {
                "deleted": len(deleted_ids),
                "deleted_jobs": deleted_ids,
                "deleted_outputs": deleted_outputs,
            }

    @app.post("/api/jobs/{kml_job_id}/snap-kml")
    def snap_kml(
        kml_job_id: str,
        req: KmlSnapRequest,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        kml_job = ctx.job_or_404(kml_job_id)
        if kml_job.status != "succeeded" or not kml_job.result:
            raise HTTPException(status_code=400, detail="KML import job has not succeeded")
        kml_path = Path(str(kml_job.result["kml_path"]))

        dem_path: Path | None = None
        raster_path: Path
        snap_mode = "min" if req.target == "dem" else "max"
        high_threshold = req.high_threshold
        if req.target == "dem":
            if not req.dem_job_id:
                raise HTTPException(
                    status_code=400, detail="dem_job_id is required for DEM snapping"
                )
            dem_job = ctx.job_or_404(req.dem_job_id)
            if dem_job.status != "succeeded" or not dem_job.result:
                raise HTTPException(status_code=400, detail="DEM job has not succeeded")
            dem_path = Path(str(dem_job.result["dem_path"]))
            raster_path = dem_path
        else:
            if not req.compare_job_id:
                raise HTTPException(
                    status_code=400, detail="compare_job_id is required for score-raster snapping"
                )
            compare_job = ctx.job_or_404(req.compare_job_id)
            if compare_job.status != "succeeded" or not compare_job.result:
                raise HTTPException(status_code=400, detail="ravine-compare job has not succeeded")
            dem_path = Path(str(compare_job.result["dem_path"]))
            raster_path = _compare_raster_path(compare_job, req.target)
            if high_threshold is None:
                high_threshold = _compare_display_high(compare_job, req.target)
        if not raster_path.exists():
            raise HTTPException(
                status_code=400, detail=f"snap raster does not exist: {raster_path}"
            )

        params = {"kml_job_id": kml_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "jobs" / f"kml-snap-{uuid.uuid4().hex[:8]}"
            kml_name = Path(str(kml_job.result.get("name") or kml_path.name)).stem
            name = f"{kml_name}-{req.target}-snapped.kml"
            progress_cb(
                f"kml-snap → {out_dir} "
                f"(target={req.target}, radius={req.search_radius_m:g}m, "
                f"high_threshold={high_threshold})"
            )
            result = snap_kml_to_raster(
                kml_path,
                raster_path,
                out_dir,
                snap_mode=snap_mode,
                search_radius_m=req.search_radius_m,
                sample_step_m=req.sample_step_m,
                high_threshold=high_threshold,
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
            bounds = (
                _raster_bounds_wgs84(dem_path) if dem_path else _raster_bounds_wgs84(raster_path)
            )
            return {
                **asdict(result),
                "name": Path(result.snapped_kml_path).name,
                "kml_path": result.snapped_kml_path,
                "kml_url": ctx.output_url(result.snapped_kml_path),
                "bounds_wgs84": bounds,
                "descent_geojson": _gdf_to_geojson(descent),
                "approach_geojson": _gdf_to_geojson(approach),
                "unclassified_geojson": _gdf_to_geojson(unclassified),
                "points_geojson": _gdf_to_geojson(point_gdf),
                "source": {
                    "kind": "kml-snap",
                    "input_kml_job_id": kml_job_id,
                    "target": req.target,
                    "raster_path": str(raster_path),
                },
            }

        job = ctx.submit("kml-snap", params, run, user=user, project_id=project_id)
        return asdict(job)

    @app.post("/api/jobs/{kml_job_id}/track-profile")
    def track_profile(
        kml_job_id: str,
        req: TrackProfileRequest,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        kml_job = ctx.job_or_404(kml_job_id)
        if kml_job.status != "succeeded" or not kml_job.result:
            raise HTTPException(status_code=400, detail="KML job has not succeeded")
        dem_job = ctx.job_or_404(req.dem_job_id)
        if dem_job.status != "succeeded" or not dem_job.result:
            raise HTTPException(status_code=400, detail="DEM job has not succeeded")

        kml_path = Path(str(kml_job.result["kml_path"]))
        dem_path = Path(str(dem_job.result["dem_path"]))
        params = {"kml_job_id": kml_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            progress_cb(
                f"track-profile {req.category}[{req.track_index}] "
                f"step={req.sample_step_m:g}m window={req.rappel_window_m:g}m"
            )
            result = analyze_track_profile(
                kml_path,
                dem_path,
                category=req.category,
                track_index=req.track_index,
                sample_step_m=req.sample_step_m,
                rappel_window_m=req.rappel_window_m,
                min_rappel_drop_m=req.min_rappel_drop_m,
                min_rappel_slope=req.min_rappel_slope,
            )
            payload = asdict(result)
            payload["rappel_candidates_geojson"] = _rappels_geojson(result.rappel_candidates)
            payload["source"] = {
                "kind": "track-profile",
                "kml_job_id": kml_job_id,
                "dem_job_id": req.dem_job_id,
            }
            return payload

        job = ctx.submit("track-profile", params, run, user=user, project_id=project_id)
        return asdict(job)

    if ctx.frontend_dist and ctx.frontend_dist.exists():
        app.mount("/", StaticFiles(directory=ctx.frontend_dist, html=True), name="frontend")

    return app


def cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-web",
        description="Run the local canyongis web interface.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    p.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    p.add_argument("--output-root", default="runs/web", help="Job/output root (default: runs/web).")
    p.add_argument(
        "--pg-host",
        default=os.environ.get("CANYONGIS_PG_HOST", "localhost"),
        help="PostgreSQL host (default: localhost).",
    )
    p.add_argument(
        "--pg-port",
        type=int,
        default=int(os.environ.get("CANYONGIS_PG_PORT", "5433")),
        help="PostgreSQL port (default: 5433).",
    )
    p.add_argument(
        "--pg-db",
        default=os.environ.get("CANYONGIS_PG_DB", "postgres"),
        help="PostgreSQL database (default: postgres).",
    )
    p.add_argument(
        "--pg-user",
        default=os.environ.get("CANYONGIS_PG_USER", os.environ.get("PGUSER", getuser())),
        help="PostgreSQL user (default: current user).",
    )
    p.add_argument(
        "--pg-password",
        default=os.environ.get("CANYONGIS_PG_PASSWORD", os.environ.get("PGPASSWORD")),
        help="PostgreSQL password (default: CANYONGIS_PG_PASSWORD or PGPASSWORD).",
    )
    p.add_argument(
        "--pg-dsn",
        default=os.environ.get("CANYONGIS_PG_DSN"),
        help="Full PostgreSQL connection string; overrides --pg-host/--pg-port/--pg-db/--pg-user.",
    )
    p.add_argument(
        "--frontend-dist",
        default=os.environ.get("CANYONGIS_FRONTEND_DIST", "web/frontend/dist"),
        help="Built frontend directory to serve if present.",
    )
    args = p.parse_args(argv)

    import uvicorn

    postgres = PostgresConfig(
        host=args.pg_host,
        port=args.pg_port,
        dbname=args.pg_db,
        user=args.pg_user,
        password=args.pg_password,
        dsn=args.pg_dsn,
    )
    app = create_app(
        output_root=args.output_root,
        frontend_dist=args.frontend_dist,
        postgres=postgres,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    cli()

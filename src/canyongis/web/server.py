from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import traceback
import urllib.error
import urllib.parse
import urllib.request
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
from rasterio.warp import transform as transform_coords

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
from .source_jobs import resync_kml_import

JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
DEFAULT_PROJECT_NAME = "Personal"
ASSET_BOUNDS_SQL = """
COALESCE(
    a.bounds_wgs84,
    CASE WHEN jsonb_typeof(a.metadata->'bounds_wgs84') = 'array'
         THEN a.metadata->'bounds_wgs84' END,
    CASE WHEN a.kind = 'raster.dem' AND jsonb_typeof(j.result->'bounds_wgs84') = 'array'
         THEN j.result->'bounds_wgs84' END
)
"""


def _coerce_job_id_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for k in ("job_id", "id"):
            v = raw.get(k)
            if isinstance(v, str) and v:
                return v
    return None


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
                bounds = cand.metadata.get("bounds_wgs84")
                cur.execute(
                    "SELECT id FROM canyongis_assets WHERE project_id = %s AND path = %s LIMIT 1",
                    (project_id, cand.path),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if bounds is not None:
                        cur.execute(
                            """
                            UPDATE canyongis_assets
                            SET bounds_wgs84 = COALESCE(bounds_wgs84, %s)
                            WHERE id = %s
                            """,
                            (Jsonb(bounds), existing[0]),
                        )
                    continue
                asset_id = new_asset_id()
                try:
                    size_bytes = Path(cand.path).stat().st_size
                except OSError:
                    size_bytes = None
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

    def register_job_inputs(self, job: Job) -> int:
        """Insert canyongis_job_inputs edges from ``params._inputs`` of ``job``.

        Each entry of ``_inputs`` maps an input slot key (e.g. "dem", "score")
        to a producing job id (or a dict containing one). We resolve that job
        to its primary asset of the kind the spec's ``InputSpec`` accepts and
        write an edge. Tools whose inputs are not assets (e.g. ``build-dem``
        takes an AOI bounds object) record no edges.
        """
        spec = get_spec(job.kind)
        if spec is None:
            return 0
        recorded = job.params.get("_inputs") if isinstance(job.params, dict) else None
        if not isinstance(recorded, dict) or not recorded:
            return 0
        if job.kind == "kml-import" and not (
            isinstance(job.result, dict) and isinstance(job.result.get("container_dem_job_id"), str)
        ):
            recorded = {k: v for k, v in recorded.items() if k != "container"}
            if not recorded:
                return 0
        input_kinds = {ispec.key: ispec.kind for ispec in spec.inputs}
        if job.kind == "kml-import":
            input_kinds.setdefault("container", "raster.dem")
        written = 0
        with self.conn.cursor() as cur:
            for input_key, raw in recorded.items():
                producer_id = _coerce_job_id_str(raw)
                if not producer_id:
                    continue
                accepted = input_kinds.get(input_key)
                asset_id = self._pick_input_asset(cur, producer_id, accepted)
                if not asset_id:
                    continue
                cur.execute(
                    "INSERT INTO canyongis_job_inputs (job_id, input_key, asset_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (job.id, input_key, asset_id),
                )
                written += cur.rowcount or 0
        return written

    @staticmethod
    def _pick_input_asset(cur, producer_job_id: str, accepted_kind: str | None) -> str | None:
        """Pick the best asset produced by ``producer_job_id`` for ``accepted_kind``.

        Match by prefix: an InputSpec.kind of ``raster.score`` matches assets
        with kind starting ``raster.score.``. Falls back to any asset from
        the job when no prefix match is available (handles legacy
        extension-kinded rows).
        """
        if accepted_kind:
            cur.execute(
                "SELECT id FROM canyongis_assets "
                "WHERE job_id = %s AND (kind = %s OR kind LIKE %s) "
                "ORDER BY created_at LIMIT 1",
                (producer_job_id, accepted_kind, f"{accepted_kind}.%"),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
        cur.execute(
            "SELECT id FROM canyongis_assets WHERE job_id = %s ORDER BY created_at LIMIT 1",
            (producer_job_id,),
        )
        row = cur.fetchone()
        return row[0] if row is not None else None

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


class RopewikiImportRequest(BaseModel):
    source: str = Field(..., min_length=1)
    container_dem_job_id: str | None = None


class KmlContainerRequest(BaseModel):
    container_dem_job_id: str | None = None


class ResolveUrlRequest(BaseModel):
    url: str = Field(..., min_length=1)


def _is_allowed_map_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().removeprefix("www.")
    return host in {"maps.app.goo.gl", "goo.gl", "google.com"} or host.endswith(".google.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _resolve_map_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _is_allowed_map_host(parsed.hostname):
        raise HTTPException(status_code=400, detail="Only Google Maps URLs can be resolved")
    opener = urllib.request.build_opener(_NoRedirect)
    current_url = url
    try:
        for _ in range(5):
            req = urllib.request.Request(
                current_url,
                headers={"User-Agent": "canyongis-web/0.1"},
            )
            try:
                with opener.open(req, timeout=8) as res:
                    return res.geturl()
            except urllib.error.HTTPError as exc:
                if exc.code < 300 or exc.code >= 400:
                    raise
                location = exc.headers.get("Location")
                if not location:
                    raise
                next_url = urllib.parse.urljoin(current_url, location)
                next_parsed = urllib.parse.urlparse(next_url)
                if next_parsed.scheme not in {"http", "https"} or not _is_allowed_map_host(
                    next_parsed.hostname
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Resolved URL was not a Google Maps URL",
                    ) from exc
                current_url = next_url
        raise HTTPException(status_code=400, detail="Too many Google Maps redirects")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not resolve URL: {exc}") from exc


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
                self.store.register_job_inputs(job)
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


def _set_kml_container_params(params: dict[str, Any], container_dem_job_id: str | None) -> None:
    params["container_dem_job_id"] = container_dem_job_id
    inputs = params.get("_inputs")
    inputs = dict(inputs) if isinstance(inputs, dict) else {}
    if container_dem_job_id:
        inputs["container"] = container_dem_job_id
    else:
        inputs.pop("container", None)
    if inputs:
        params["_inputs"] = inputs
    else:
        params.pop("_inputs", None)


def _assign_kml_container_if_valid(
    ctx: WebContext,
    result: dict[str, Any],
    params: dict[str, Any],
    container_dem_job_id: str | None,
    progress_cb,
) -> None:
    if not container_dem_job_id:
        _set_kml_container_params(params, None)
        result.pop("container_dem_job_id", None)
        return
    try:
        _validate_kml_container(ctx, container_dem_job_id, result["bounds_wgs84"])
    except Exception as exc:
        progress_cb(f"KML not assigned to DEM container {container_dem_job_id}: {exc}")
        _set_kml_container_params(params, None)
        result.pop("container_dem_job_id", None)
        return
    _set_kml_container_params(params, container_dem_job_id)
    result["container_dem_job_id"] = container_dem_job_id


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
        _require_project_member(ctx, project_id, user)
        return project_id
    return ctx.store.ensure_personal_project(user)


def _require_project_member(ctx: WebContext, project_id: str, user: User) -> None:
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


def _coerce_wgs84_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        west, south, east, north = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        return None
    return west, south, east, north


def _bounds_contain_lng_lat(
    bounds: tuple[float, float, float, float], lng: float, lat: float
) -> bool:
    west, south, east, north = bounds
    return west <= lng <= east and south <= lat <= north


def _bounds_area(bounds: tuple[float, float, float, float]) -> float:
    west, south, east, north = bounds
    return max(0.0, east - west) * max(0.0, north - south)


def _visible_dem_assets_for_point(
    ctx: WebContext, project_id: str, *, lat: float, lng: float
) -> list[dict[str, Any]]:
    with ctx.store.conn.cursor() as cur:
        cur.execute(
            f"""
            WITH visible_dem_assets AS (
                SELECT a.id, a.path, {ASSET_BOUNDS_SQL} AS bounds_wgs84,
                       COALESCE(a.metadata->>'label', j.result->>'name', a.id) AS label,
                       a.created_at
                FROM canyongis_assets a
                LEFT JOIN canyongis_jobs j ON j.id = a.job_id
                WHERE a.project_id = %s AND a.deleted_at IS NULL AND a.kind = 'raster.dem'
                  AND NOT EXISTS (
                      SELECT 1 FROM canyongis_project_hidden_assets h
                      WHERE h.project_id = %s AND h.asset_id = a.id
                  )
                UNION ALL
                SELECT a.id, a.path, {ASSET_BOUNDS_SQL} AS bounds_wgs84,
                       COALESCE(a.metadata->>'label', j.result->>'name', a.id) AS label,
                       a.created_at
                FROM canyongis_assets a
                JOIN canyongis_project_asset_links l ON l.asset_id = a.id
                LEFT JOIN canyongis_jobs j ON j.id = a.job_id
                WHERE l.project_id = %s AND a.deleted_at IS NULL AND a.kind = 'raster.dem'
            )
            SELECT id, path, bounds_wgs84, label, created_at
            FROM visible_dem_assets
            WHERE path IS NOT NULL
            """,
            (project_id, project_id, project_id),
        )
        candidates: list[dict[str, Any]] = []
        for asset_id, path, raw_bounds, label, created_at in cur.fetchall():
            bounds = _coerce_wgs84_bounds(raw_bounds)
            if bounds is None or not _bounds_contain_lng_lat(bounds, lng, lat):
                continue
            candidates.append(
                {
                    "asset_id": asset_id,
                    "path": path,
                    "bounds": bounds,
                    "label": label or asset_id,
                    "created_at": created_at,
                }
            )
    candidates.sort(key=lambda row: str(row["created_at"] or ""), reverse=True)
    candidates.sort(key=lambda row: _bounds_area(row["bounds"]))
    return candidates


def _sample_dem_elevation_m(path: str, *, lat: float, lng: float) -> float | None:
    dem_path = Path(path)
    if not dem_path.exists():
        return None
    try:
        with rasterio.open(dem_path) as src:
            if src.crs is not None:
                projected = transform_coords("EPSG:4326", src.crs, [lng], [lat])
                xs, ys = projected[0], projected[1]
                x, y = float(xs[0]), float(ys[0])
            else:
                x, y = lng, lat
            row, col = src.index(x, y)
            if row < 0 or row >= src.height or col < 0 or col >= src.width:
                return None
            sample = next(src.sample([(x, y)], masked=True))
            mask = getattr(sample, "mask", None)
            if mask is not None and bool(mask[0]):
                return None
            value = float(sample[0])
            if src.nodata is not None and value == float(src.nodata):
                return None
            return value if math.isfinite(value) else None
    except Exception:
        return None


def _load_asset_summary(cur, asset_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"""
        SELECT a.id, a.kind, a.path, a.url, a.job_id, {ASSET_BOUNDS_SQL}
        FROM canyongis_assets a
        LEFT JOIN canyongis_jobs j ON j.id = a.job_id
        WHERE a.id = %s
        """,
        (asset_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "kind": row[1],
        "path": row[2],
        "url": row[3],
        "job_id": row[4],
        "bounds_wgs84": row[5],
    }


def _walk_lineage_upward(
    cur, root_id: str, nodes: dict[str, dict[str, Any]], edges: list[dict[str, str]]
) -> None:
    queue: list[str] = [root_id]
    while queue:
        cur_id = queue.pop()
        if cur_id in nodes:
            continue
        summary = _load_asset_summary(cur, cur_id)
        if summary is None:
            continue
        nodes[cur_id] = summary
        job_id = summary["job_id"]
        if not job_id:
            continue
        cur.execute(
            "SELECT input_key, asset_id FROM canyongis_job_inputs WHERE job_id = %s",
            (job_id,),
        )
        for input_key, producer_asset_id in cur.fetchall():
            edges.append(
                {"consumer": cur_id, "input_key": input_key, "producer": producer_asset_id}
            )
            if producer_asset_id not in nodes:
                queue.append(producer_asset_id)


def _walk_lineage_downward(
    cur, root_id: str, nodes: dict[str, dict[str, Any]], edges: list[dict[str, str]]
) -> None:
    queue: list[str] = [root_id]
    seen_edges: set[tuple[str, str, str]] = {
        (e["consumer"], e["input_key"], e["producer"]) for e in edges
    }
    while queue:
        cur_id = queue.pop()
        if cur_id not in nodes:
            summary = _load_asset_summary(cur, cur_id)
            if summary is None:
                continue
            nodes[cur_id] = summary
        cur.execute(
            """
            SELECT i.input_key, i.job_id, a.id
            FROM canyongis_job_inputs i
            JOIN canyongis_assets a ON a.job_id = i.job_id
            WHERE i.asset_id = %s
            """,
            (cur_id,),
        )
        for input_key, _consumer_job_id, consumer_asset_id in cur.fetchall():
            edge_key = (consumer_asset_id, input_key, cur_id)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append(
                {"consumer": consumer_asset_id, "input_key": input_key, "producer": cur_id}
            )
            if consumer_asset_id not in nodes:
                queue.append(consumer_asset_id)


def _require_asset_access(ctx: WebContext, asset_id: str, user: User) -> None:
    """Raise 404 if the asset is invisible to ``user``.

    Visible = the user created it, the asset's owning project has the user as
    a member, OR the asset is linked into some project the user is a member of.
    """
    with ctx.store.conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM canyongis_assets a
            WHERE a.id = %s AND (
                a.created_by_user_id = %s
                OR
                EXISTS (
                    SELECT 1 FROM canyongis_project_memberships m
                    WHERE m.project_id = a.project_id AND m.user_id = %s
                )
                OR EXISTS (
                    SELECT 1 FROM canyongis_project_asset_links l
                    JOIN canyongis_project_memberships m ON m.project_id = l.project_id
                    WHERE l.asset_id = a.id AND m.user_id = %s
                )
            )
            LIMIT 1
            """,
            (asset_id, user.id, user.id, user.id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")


def _asset_projects(cur, asset_ids: list[str], user: User) -> dict[str, list[dict[str, str]]]:
    if not asset_ids:
        return {}
    cur.execute(
        """
        SELECT DISTINCT x.asset_id, x.project_id, p.name
        FROM (
            SELECT a.id AS asset_id, a.project_id
            FROM canyongis_assets a
            WHERE a.id = ANY(%s)
            UNION
            SELECT l.asset_id, l.project_id
            FROM canyongis_project_asset_links l
            JOIN canyongis_project_memberships m ON m.project_id = l.project_id
            WHERE l.asset_id = ANY(%s) AND m.user_id = %s
        ) x
        JOIN canyongis_projects p ON p.id = x.project_id
        ORDER BY p.name, x.project_id
        """,
        (asset_ids, asset_ids, user.id),
    )
    projects: dict[str, list[dict[str, str]]] = {}
    for asset_id, project_id, project_name in cur.fetchall():
        projects.setdefault(asset_id, []).append({"id": project_id, "name": project_name})
    return projects


ActiveProjectDep = Depends(_active_project_id)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class AssetLinkRequest(BaseModel):
    asset_id: str = Field(..., min_length=1)


class ProjectRequest(BaseModel):
    name: str = Field(..., min_length=1)


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

    @app.post("/api/resolve-map-url")
    def resolve_map_url(req: ResolveUrlRequest, _user: User = CurrentUserDep) -> dict[str, str]:
        return {"url": _resolve_map_url(req.url)}

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

    @app.post("/api/projects")
    def create_project(req: ProjectRequest, user: User = CurrentUserDep) -> dict[str, Any]:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="project name is required")
        project_id = uuid.uuid4().hex[:16]
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
        return {"id": project_id, "name": name, "role": "owner"}

    @app.patch("/api/projects/{project_id}")
    def update_project(
        project_id: str, req: ProjectRequest, user: User = CurrentUserDep
    ) -> dict[str, Any]:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="project name is required")
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM canyongis_project_memberships WHERE project_id = %s AND user_id = %s",
                (project_id, user.id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"project {project_id} not found")
            role = row[0]
            if role == "viewer":
                raise HTTPException(status_code=403, detail="viewers cannot rename projects")
            cur.execute("UPDATE canyongis_projects SET name = %s WHERE id = %s", (name, project_id))
        return {"id": project_id, "name": name, "role": role}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        """Hide a project without deleting its library assets."""
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM canyongis_project_memberships WHERE project_id = %s AND user_id = %s",
                (project_id, user.id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"project {project_id} not found")
            role = row[0]
            if role == "owner":
                cur.execute(
                    "DELETE FROM canyongis_project_asset_links WHERE project_id = %s",
                    (project_id,),
                )
                cur.execute(
                    "DELETE FROM canyongis_project_memberships WHERE project_id = %s",
                    (project_id,),
                )
            else:
                cur.execute(
                    "DELETE FROM canyongis_project_memberships WHERE project_id = %s AND user_id = %s",
                    (project_id, user.id),
                )
        return {"deleted": True, "project_id": project_id}

    @app.get("/api/assets")
    def list_asset_library(kind: str | None = None, user: User = CurrentUserDep) -> dict[str, Any]:
        filters = [
            "a.deleted_at IS NULL",
            """
            (
                a.created_by_user_id = %s
                OR EXISTS (
                    SELECT 1 FROM canyongis_project_memberships m
                    WHERE m.project_id = a.project_id AND m.user_id = %s
                )
                OR EXISTS (
                    SELECT 1 FROM canyongis_project_asset_links l
                    JOIN canyongis_project_memberships m ON m.project_id = l.project_id
                    WHERE l.asset_id = a.id AND m.user_id = %s
                )
            )
            """,
        ]
        params: list[Any] = [user.id, user.id, user.id]
        if kind:
            filters.append("(a.kind = %s OR a.kind LIKE %s)")
            params.extend([kind, f"{kind}.%"])
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (a.id)
                           a.id, a.project_id, p.name, a.job_id, a.kind, a.path, a.url,
                           {ASSET_BOUNDS_SQL}, a.format, a.media_type, a.size_bytes,
                           a.metadata, a.created_at, FALSE AS linked, NULL::text AS linked_at
                    FROM canyongis_assets a
                    JOIN canyongis_projects p ON p.id = a.project_id
                    LEFT JOIN canyongis_jobs j ON j.id = a.job_id
                    WHERE {" AND ".join(filters)}
                    ORDER BY a.id, a.created_at DESC
                ) assets
                ORDER BY created_at DESC
                """,
                params,
            )
            rows = cur.fetchall()
            projects_by_asset = _asset_projects(cur, [row[0] for row in rows], user)
        assets = [
            {
                "id": row[0],
                "project_id": row[1],
                "project_name": row[2],
                "job_id": row[3],
                "kind": row[4],
                "path": row[5],
                "url": row[6],
                "bounds_wgs84": row[7],
                "format": row[8],
                "media_type": row[9],
                "size_bytes": row[10],
                "metadata": row[11] or {},
                "created_at": row[12],
                "linked": row[13],
                "linked_at": row[14],
                "projects": projects_by_asset.get(row[0], []),
            }
            for row in rows
        ]
        return {"assets": assets}

    @app.get("/api/projects/{project_id}/assets")
    def list_project_assets(
        project_id: str,
        kind: str | None = None,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        """Assets visible in the project: primary rows + linked rows.

        ``kind`` is a prefix match against the typed taxonomy
        (``raster.score`` matches ``raster.score.ravine.tpi`` etc.). Linked
        rows are flagged with ``linked: true`` and carry the originating
        project's name on the row. Assets hidden in this project via
        ``canyongis_project_hidden_assets`` are excluded.
        """
        _require_project_member(ctx, project_id, user)
        params: list[Any] = [project_id, project_id, project_id]
        sql = f"""
            SELECT a.id, a.project_id, p.name, a.job_id, a.kind, a.path, a.url,
                   {ASSET_BOUNDS_SQL}, a.format, a.media_type, a.size_bytes,
                   a.metadata, a.created_at, FALSE AS linked, NULL::text AS linked_at
            FROM canyongis_assets a
            JOIN canyongis_projects p ON p.id = a.project_id
            LEFT JOIN canyongis_jobs j ON j.id = a.job_id
            WHERE a.project_id = %s AND a.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM canyongis_project_hidden_assets h
                  WHERE h.project_id = %s AND h.asset_id = a.id
              )
            UNION ALL
            SELECT a.id, a.project_id, p.name, a.job_id, a.kind, a.path, a.url,
                   {ASSET_BOUNDS_SQL}, a.format, a.media_type, a.size_bytes,
                   a.metadata, a.created_at, TRUE AS linked, l.linked_at
            FROM canyongis_assets a
            JOIN canyongis_projects p ON p.id = a.project_id
            JOIN canyongis_project_asset_links l ON l.asset_id = a.id
            LEFT JOIN canyongis_jobs j ON j.id = a.job_id
            WHERE l.project_id = %s AND a.deleted_at IS NULL
        """
        if kind:
            sql = f"SELECT * FROM ({sql}) sub WHERE kind = %s OR kind LIKE %s"
            params.extend([kind, f"{kind}.%"])
        sql += " ORDER BY created_at DESC"
        with ctx.store.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            projects_by_asset = _asset_projects(cur, [row[0] for row in rows], user)
        assets = [
            {
                "id": row[0],
                "project_id": row[1],
                "project_name": row[2],
                "job_id": row[3],
                "kind": row[4],
                "path": row[5],
                "url": row[6],
                "bounds_wgs84": row[7],
                "format": row[8],
                "media_type": row[9],
                "size_bytes": row[10],
                "metadata": row[11] or {},
                "created_at": row[12],
                "linked": row[13],
                "linked_at": row[14],
                "projects": projects_by_asset.get(row[0], []),
            }
            for row in rows
        ]
        return {"assets": assets}

    @app.post("/api/projects/{project_id}/assets/link")
    def link_project_asset(
        project_id: str,
        req: AssetLinkRequest,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        _require_asset_access(ctx, req.asset_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM canyongis_assets WHERE id = %s AND deleted_at IS NULL",
                (req.asset_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"asset {req.asset_id} not found")
            if row[0] == project_id:
                return {"linked": False, "asset_id": req.asset_id, "project_id": project_id}
            cur.execute(
                """
                INSERT INTO canyongis_project_asset_links
                    (project_id, asset_id, linked_by_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (project_id, req.asset_id, user.id),
            )
            linked = (cur.rowcount or 0) > 0
        return {"linked": linked, "asset_id": req.asset_id, "project_id": project_id}

    @app.delete("/api/projects/{project_id}/assets/{asset_id}")
    def unlink_project_asset(
        project_id: str,
        asset_id: str,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM canyongis_project_asset_links WHERE project_id = %s AND asset_id = %s",
                (project_id, asset_id),
            )
            deleted = (cur.rowcount or 0) > 0
        return {"deleted": deleted, "asset_id": asset_id, "project_id": project_id}

    @app.post("/api/projects/{project_id}/assets/{asset_id}/hide")
    def hide_project_asset(
        project_id: str,
        asset_id: str,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        """Hide an asset from the project view without deleting it.

        For linked assets, unlinks instead (so the asset library still owns the
        underlying record). For native (project-owned) assets, inserts into
        ``canyongis_project_hidden_assets`` so it is filtered out of project
        listings. The asset library is unaffected.
        """
        _require_project_member(ctx, project_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM canyongis_assets WHERE id = %s AND deleted_at IS NULL",
                (asset_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
            owner_project = row[0]
            if owner_project != project_id:
                cur.execute(
                    "DELETE FROM canyongis_project_asset_links "
                    "WHERE project_id = %s AND asset_id = %s",
                    (project_id, asset_id),
                )
                return {
                    "hidden": (cur.rowcount or 0) > 0,
                    "mode": "unlinked",
                    "asset_id": asset_id,
                    "project_id": project_id,
                }
            cur.execute(
                """
                INSERT INTO canyongis_project_hidden_assets (project_id, asset_id, hidden_by_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (project_id, asset_id, user.id),
            )
            inserted = (cur.rowcount or 0) > 0
        return {
            "hidden": inserted,
            "mode": "hidden",
            "asset_id": asset_id,
            "project_id": project_id,
        }

    @app.get("/api/projects/{project_id}/assets/hidden")
    def list_hidden_project_assets(project_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM canyongis_project_hidden_assets WHERE project_id = %s",
                (project_id,),
            )
            ids = [row[0] for row in cur.fetchall()]
        return {"asset_ids": ids}

    @app.get("/api/projects/{project_id}/elevation")
    def project_elevation_probe(
        project_id: str,
        lat: float,
        lng: float,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        if not (
            math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        ):
            raise HTTPException(status_code=400, detail="invalid lat/lng")

        first_match: dict[str, Any] | None = None
        for candidate in _visible_dem_assets_for_point(ctx, project_id, lat=lat, lng=lng):
            if first_match is None:
                first_match = candidate
            elevation = _sample_dem_elevation_m(str(candidate["path"]), lat=lat, lng=lng)
            if elevation is not None:
                return {
                    "lat": lat,
                    "lng": lng,
                    "elevation_m": elevation,
                    "asset_id": candidate["asset_id"],
                    "asset_label": candidate["label"],
                }

        return {
            "lat": lat,
            "lng": lng,
            "elevation_m": None,
            "asset_id": first_match["asset_id"] if first_match else None,
            "asset_label": first_match["label"] if first_match else None,
        }

    @app.post("/api/projects/{project_id}/assets/{asset_id}/unhide")
    def unhide_project_asset(
        project_id: str,
        asset_id: str,
        user: User = CurrentUserDep,
    ) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM canyongis_project_hidden_assets "
                "WHERE project_id = %s AND asset_id = %s",
                (project_id, asset_id),
            )
            unhidden = (cur.rowcount or 0) > 0
        return {"unhidden": unhidden, "asset_id": asset_id, "project_id": project_id}

    @app.get("/api/projects/{project_id}/pipeline")
    def project_pipeline(project_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        _require_project_member(ctx, project_id, user)
        with ctx.store.conn.cursor() as cur:
            cur.execute(
                """
                WITH visible_assets AS (
                    SELECT a.id, a.job_id, a.kind, a.path, a.url, a.metadata, a.created_at
                    FROM canyongis_assets a
                    WHERE a.project_id = %s AND a.deleted_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM canyongis_project_hidden_assets h
                          WHERE h.project_id = %s AND h.asset_id = a.id
                      )
                    UNION
                    SELECT a.id, a.job_id, a.kind, a.path, a.url, a.metadata, a.created_at
                    FROM canyongis_assets a
                    JOIN canyongis_project_asset_links l ON l.asset_id = a.id
                    WHERE l.project_id = %s AND a.deleted_at IS NULL
                )
                SELECT id, job_id, kind, path, url, metadata, created_at
                FROM visible_assets
                ORDER BY created_at
                """,
                (project_id, project_id, project_id),
            )
            assets = [
                {
                    "id": row[0],
                    "job_id": row[1],
                    "kind": row[2],
                    "path": row[3],
                    "url": row[4],
                    "metadata": row[5] or {},
                    "created_at": row[6],
                }
                for row in cur.fetchall()
            ]
            cur.execute(
                """
                WITH visible_assets AS (
                    SELECT a.id, a.job_id FROM canyongis_assets a
                    WHERE a.project_id = %s AND a.deleted_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM canyongis_project_hidden_assets h
                          WHERE h.project_id = %s AND h.asset_id = a.id
                      )
                    UNION
                    SELECT a.id, a.job_id FROM canyongis_assets a
                    JOIN canyongis_project_asset_links l ON l.asset_id = a.id
                    WHERE l.project_id = %s AND a.deleted_at IS NULL
                )
                SELECT DISTINCT consumer.id, i.input_key, producer.id
                FROM canyongis_job_inputs i
                JOIN visible_assets producer ON producer.id = i.asset_id
                JOIN visible_assets consumer ON consumer.job_id = i.job_id
                ORDER BY consumer.id, i.input_key, producer.id
                """,
                (project_id, project_id, project_id),
            )
            edges = [
                {"consumer": row[0], "input_key": row[1], "producer": row[2]}
                for row in cur.fetchall()
            ]
        return {"assets": assets, "edges": edges}

    @app.get("/api/assets/{asset_id}/lineage")
    def asset_lineage(asset_id: str, user: User = CurrentUserDep) -> dict[str, Any]:
        """Return the DAG of ancestors and descendants of ``asset_id``.

        Shape matches UI_DESIGN.md §5.1: ``{root, nodes, edges}`` where
        each edge is ``{consumer, input_key, producer}``. ``nodes`` is a
        map of asset_id to summary; ``edges`` walks the graph in both
        directions from the root.
        """
        _require_asset_access(ctx, asset_id, user)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        with ctx.store.conn.cursor() as cur:
            _walk_lineage_upward(cur, asset_id, nodes, edges)
            _walk_lineage_downward(cur, asset_id, nodes, edges)
        return {"root": asset_id, "nodes": nodes, "edges": edges}

    @app.get("/api/tools")
    def tools(user: User = CurrentUserDep) -> dict[str, Any]:
        builtins = [
            {"id": "ravine-spine", "label": "Ravine spine from TPI"},
            {"id": "kml-snap", "label": "Snap KML to terrain"},
            {"id": "track-profile", "label": "Track elevation profile"},
        ]
        registered = [{"id": s.key, "label": s.label} for s in list_specs()]
        seen = {tool["id"] for tool in registered}
        return {"tools": [*registered, *(tool for tool in builtins if tool["id"] not in seen)]}

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
                    "runner_kind": spec.runner_kind,
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
        params: dict[str, Any] = {
            "name": name,
            "filename": original_name,
            "container_dem_job_id": container_dem_job_id,
        }
        if container_dem_job_id:
            params["_inputs"] = {"container": container_dem_job_id}

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
            )
            _assign_kml_container_if_valid(ctx, result, params, container_dem_job_id, progress_cb)
            return result

        job = ctx.submit(
            "kml-import", params, run, user=user, project_id=project_id, include_job_id=True
        )
        return asdict(job)

    @app.post("/api/jobs/import-kml-ropewiki")
    def import_kml_ropewiki(
        req: RopewikiImportRequest, user: User = CurrentUserDep, project_id: str = ActiveProjectDep
    ) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        if req.container_dem_job_id:
            inputs["container"] = req.container_dem_job_id
        try:
            job = submit_tool_job(
                ctx,
                "kml-import",
                {"params": req.model_dump(), "inputs": inputs},
                user=user,
                project_id=project_id,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return asdict(job)

    @app.post("/api/jobs/import-kml-ropewiki-bulk")
    def import_kml_ropewiki_bulk(
        body: Annotated[dict[str, Any] | None, Body()] = None,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        try:
            job = submit_tool_job(
                ctx, "ropewiki-bulk-import", body, user=user, project_id=project_id
            )
        except DispatchError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return asdict(job)

    @app.post("/api/jobs/{kml_job_id}/container")
    def set_kml_container(
        kml_job_id: str,
        req: KmlContainerRequest,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        job = ctx.job_or_404(kml_job_id)
        if job.kind != "kml-import":
            raise HTTPException(status_code=400, detail="job is not a kml-import")
        if job.status != "succeeded" or not job.result:
            raise HTTPException(status_code=400, detail="kml-import job has not succeeded")
        if job.project_id != project_id:
            raise HTTPException(status_code=404, detail=f"job not found: {kml_job_id}")
        new_container = req.container_dem_job_id or None
        if new_container:
            bounds = job.result.get("bounds_wgs84")
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise HTTPException(status_code=400, detail="kml-import is missing bounds")
            try:
                _validate_kml_container(ctx, new_container, bounds)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        with ctx.lock:
            params = dict(job.params) if isinstance(job.params, dict) else {}
            params["container_dem_job_id"] = new_container
            existing_inputs = params.get("_inputs")
            inputs = dict(existing_inputs) if isinstance(existing_inputs, dict) else {}
            if new_container:
                inputs["container"] = new_container
            else:
                inputs.pop("container", None)
            params["_inputs"] = inputs
            job.params = params
            result = dict(job.result) if isinstance(job.result, dict) else {}
            if new_container:
                result["container_dem_job_id"] = new_container
            else:
                result.pop("container_dem_job_id", None)
            job.result = result
            job.updated_at = ctx.now()
            ctx.store.upsert(job)
            with ctx.store.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM canyongis_job_inputs WHERE job_id = %s AND input_key = %s",
                    (kml_job_id, "container"),
                )
            if new_container:
                ctx.store.register_job_inputs(job)
        return asdict(job)

    @app.post("/api/jobs/{kml_job_id}/sync-kml")
    def sync_kml_import_endpoint(
        kml_job_id: str,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        job = ctx.job_or_404(kml_job_id)
        if job.kind != "kml-import":
            raise HTTPException(status_code=400, detail="job is not a kml-import")
        if job.status != "succeeded" or not job.result:
            raise HTTPException(status_code=400, detail="kml-import job has not succeeded")
        if job.project_id != project_id:
            raise HTTPException(status_code=404, detail=f"job not found: {kml_job_id}")
        existing_source = job.result.get("source")
        if not isinstance(existing_source, dict):
            raise HTTPException(status_code=400, detail="kml-import has no source metadata")
        if existing_source.get("type") not in ("ropewiki", "caltopo", "url"):
            raise HTTPException(
                status_code=400,
                detail="kml-import was not created from a URL and cannot be synced",
            )
        kml_path_str = job.result.get("kml_path")
        if not isinstance(kml_path_str, str) or not kml_path_str:
            raise HTTPException(status_code=400, detail="kml-import has no stored KML path")
        kml_path = Path(kml_path_str)
        progress: list[str] = []
        try:
            rebuilt = resync_kml_import(
                ctx,
                job_id=kml_job_id,
                source=existing_source,
                kml_path=kml_path,
                progress_cb=progress.append,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
        with ctx.lock:
            now = ctx.now()
            new_result = dict(job.result)
            for key in (
                "kml_path",
                "kml_url",
                "out_dir",
                "bounds_wgs84",
                "descent_geojson",
                "approach_geojson",
                "unclassified_geojson",
                "points_geojson",
                "counts",
            ):
                if key in rebuilt:
                    new_result[key] = rebuilt[key]
            merged_source = dict(rebuilt.get("source") or {})
            merged_source["synced_at"] = now
            new_result["source"] = merged_source
            job.result = new_result
            job.updated_at = now
            existing_progress = list(job.progress or [])
            existing_progress.extend(progress)
            job.progress = existing_progress[-PROGRESS_CAP:]
            ctx.store.upsert(job)
            bounds = new_result.get("bounds_wgs84")
            try:
                size_bytes = kml_path.stat().st_size
            except OSError:
                size_bytes = None
            with ctx.store.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE canyongis_assets
                    SET bounds_wgs84 = %s, size_bytes = COALESCE(%s, size_bytes)
                    WHERE job_id = %s AND path = %s
                    """,
                    (
                        Jsonb(bounds) if isinstance(bounds, list) else None,
                        size_bytes,
                        kml_job_id,
                        str(kml_path),
                    ),
                )
        return _job_response(ctx, job)

    @app.post("/api/jobs/build-dem")
    def build_dem(
        body: Annotated[dict[str, Any] | None, Body()] = None,
        user: User = CurrentUserDep,
        project_id: str = ActiveProjectDep,
    ) -> dict[str, Any]:
        try:
            job = submit_tool_job(
                ctx,
                "build-dem",
                body,
                user=user,
                project_id=project_id,
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

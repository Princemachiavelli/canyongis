from __future__ import annotations

import argparse
import json
import os
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import rasterio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from rasterio.warp import transform_bounds

from ..aoi_mosaic import build_aoi_dem_mosaic
from ..drainage_preview import export_drainage_previews
from ..ravine_compare import export_ravine_compare
from ..ravine_spine import export_ravine_spines

JobStatus = Literal["queued", "running", "succeeded", "failed"]


PROGRESS_CAP = 200


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus
    created_at: str
    updated_at: str
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    progress: list[str] = field(default_factory=list)


@dataclass
class JobStore:
    root: Path
    jobs: dict[str, Job] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            rows = json.loads(self.path.read_text())
            self.jobs = {row["id"]: Job(**row) for row in rows}

    @property
    def path(self) -> Path:
        return self.root / "jobs.json"

    def save(self) -> None:
        rows = [asdict(job) for job in sorted(self.jobs.values(), key=lambda item: item.created_at)]
        self.path.write_text(json.dumps(rows, indent=2) + "\n")


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
    name: str = Field(default="aoi", pattern=r"^[A-Za-z0-9_.-]+$")
    bounds: Bounds
    buffer_m: float = 200.0
    max_products: int = 24


class DrainageRequest(BaseModel):
    thresholds: list[int] = Field(default_factory=lambda: [200, 500, 1000, 5000])
    line_width_px: int = 2
    fill_depressions: bool = True


class RavineCompareRequest(BaseModel):
    scales_m: list[float] = Field(default_factory=lambda: [5.0, 10.0, 30.0, 100.0])
    combine: str = "normalized_sum"
    smooth_m: float = 2.0
    norm_pct: float = 99.5
    nprocs: int | None = None


class RavineSpineRequest(BaseModel):
    threshold_pct: float = 94.0
    hysteresis_ratio: float = 0.75
    branch_keep_ratio: float = 0.20
    max_path_slope: float = 0.30


class WebContext:
    def __init__(self, output_root: Path, frontend_dist: Path | None = None) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.dem_cache = self.output_root / "dem-cache"
        self.store = JobStore(self.output_root)
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.frontend_dist = frontend_dist

    def now(self) -> str:
        return datetime.now(UTC).isoformat()

    def submit(self, kind: str, params: dict[str, Any], fn) -> Job:
        """Submit a job. fn is called as fn(progress_cb) on a worker thread.
        progress_cb appends a line to the job's progress log."""
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            status="queued",
            created_at=self.now(),
            updated_at=self.now(),
            params=params,
        )
        with self.lock:
            self.store.jobs[job.id] = job
            self.store.save()
        self.executor.submit(self._run, job.id, fn)
        return job

    def _run(self, job_id: str, fn) -> None:
        self._update(job_id, status="running")

        def progress_cb(line: str) -> None:
            self.append_progress(job_id, line)

        try:
            result = fn(progress_cb)
        except Exception as exc:  # pragma: no cover - exercised by real jobs
            self._update(job_id, status="failed", error=f"{exc}\n{traceback.format_exc()}")
            return
        self._update(job_id, status="succeeded", result=result)

    def _update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock:
            job = self.store.jobs[job_id]
            job.status = status
            job.updated_at = self.now()
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            self.store.save()

    def append_progress(self, job_id: str, line: str) -> None:
        with self.lock:
            job = self.store.jobs.get(job_id)
            if job is None:
                return
            job.progress.append(line)
            if len(job.progress) > PROGRESS_CAP:
                job.progress = job.progress[-PROGRESS_CAP:]
            job.updated_at = self.now()
            self.store.save()

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


def _raster_bounds_wgs84(path: str | Path) -> tuple[float, float, float, float]:
    with rasterio.open(path) as src:
        bounds = src.bounds
        return tuple(
            float(v)
            for v in transform_bounds(
                src.crs, "EPSG:4326", bounds.left, bounds.bottom, bounds.right, bounds.top
            )
        )


def create_app(
    *,
    output_root: str | Path = "runs/web",
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    if frontend_dist is None:
        frontend_dist = os.environ.get("CANYONGIS_FRONTEND_DIST", "web/frontend/dist")
    ctx = WebContext(
        Path(output_root),
        Path(frontend_dist) if frontend_dist is not None else None,
    )
    app = FastAPI(title="canyongis web", version="0.1.0")
    app.state.ctx = ctx
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

    @app.get("/api/tools")
    def tools() -> dict[str, Any]:
        return {
            "tools": [
                {"id": "build-dem", "label": "Build DEM mosaic"},
                {"id": "drainage", "label": "Flow / drainage preview"},
                {"id": "ravine-compare", "label": "TPI / ravine compare"},
                {"id": "ravine-spine", "label": "Ravine spine from TPI"},
            ]
        }

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        with ctx.lock:
            jobs = [asdict(job) for job in ctx.store.jobs.values()]
        return {"jobs": sorted(jobs, key=lambda row: row["created_at"], reverse=True)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return asdict(ctx.job_or_404(job_id))

    @app.post("/api/jobs/build-dem")
    def build_dem(req: BuildDemRequest) -> dict[str, Any]:
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

        job = ctx.submit("build-dem", params, run)
        return asdict(job)

    @app.post("/api/jobs/{dem_job_id}/drainage")
    def drainage(dem_job_id: str, req: DrainageRequest) -> dict[str, Any]:
        dem_job = ctx.job_or_404(dem_job_id)
        if dem_job.status != "succeeded" or not dem_job.result:
            raise HTTPException(status_code=400, detail="DEM job has not succeeded")
        dem_path = dem_job.result["dem_path"]
        params = {"dem_job_id": dem_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "jobs" / f"drainage-{uuid.uuid4().hex[:8]}"
            progress_cb(f"drainage-preview → {out_dir} (thresholds={req.thresholds})")
            result = export_drainage_previews(
                dem_path,
                out_dir,
                thresholds=tuple(req.thresholds),
                fill_depressions=req.fill_depressions,
                line_width_px=req.line_width_px,
            )
            bounds = _raster_bounds_wgs84(dem_path)
            return {
                **asdict(result),
                "bounds_wgs84": bounds,
                "flow_preview_url": ctx.output_url(result.flow_preview_path),
                "stream_preview_url": ctx.output_url(result.stream_preview_path),
            }

        job = ctx.submit("drainage", params, run)
        return asdict(job)

    @app.post("/api/jobs/{dem_job_id}/ravine-compare")
    def ravine_compare(dem_job_id: str, req: RavineCompareRequest) -> dict[str, Any]:
        dem_job = ctx.job_or_404(dem_job_id)
        if dem_job.status != "succeeded" or not dem_job.result:
            raise HTTPException(status_code=400, detail="DEM job has not succeeded")
        dem_path = dem_job.result["dem_path"]
        params = {"dem_job_id": dem_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "jobs" / f"ravine-compare-{uuid.uuid4().hex[:8]}"
            result = export_ravine_compare(
                dem_path,
                out_dir,
                tpi_scales_m=tuple(req.scales_m),
                tpi_combine=req.combine,
                tpi_smooth_m=req.smooth_m,
                tpi_norm_pct=req.norm_pct,
                nprocs=req.nprocs,
                log_callback=progress_cb,
            )
            bounds = _raster_bounds_wgs84(dem_path)
            return {
                "dem_path": dem_path,
                "out_dir": str(out_dir),
                "bounds_wgs84": bounds,
                "b_tpi_path": str(out_dir / "b_tpi.tif"),
                "compare_png_url": ctx.output_url(result.compare_png_path),
                "panel_b_tpi_url": ctx.output_url(out_dir / "panel_b_tpi.png"),
            }

        job = ctx.submit("ravine-compare", params, run)
        return asdict(job)

    @app.post("/api/jobs/{compare_job_id}/ravine-spine")
    def ravine_spine(compare_job_id: str, req: RavineSpineRequest) -> dict[str, Any]:
        compare_job = ctx.job_or_404(compare_job_id)
        if compare_job.status != "succeeded" or not compare_job.result:
            raise HTTPException(status_code=400, detail="ravine-compare job has not succeeded")
        params = {"compare_job_id": compare_job_id, **req.model_dump()}

        def run(progress_cb) -> dict[str, Any]:
            out_dir = ctx.output_root / "jobs" / f"ravine-spine-{uuid.uuid4().hex[:8]}"
            progress_cb(f"ravine-spine → {out_dir}")
            result = export_ravine_spines(
                compare_job.result["b_tpi_path"],
                compare_job.result["dem_path"],
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
                "bounds_wgs84": compare_job.result["bounds_wgs84"],
                "spines_kml_url": ctx.output_url(out_dir / "spines.kml"),
                "rappel_candidates_kml_url": ctx.output_url(out_dir / "rappel_candidates.kml"),
            }

        job = ctx.submit("ravine-spine", params, run)
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
        "--frontend-dist",
        default=os.environ.get("CANYONGIS_FRONTEND_DIST", "web/frontend/dist"),
        help="Built frontend directory to serve if present.",
    )
    args = p.parse_args(argv)

    import uvicorn

    app = create_app(output_root=args.output_root, frontend_dist=args.frontend_dist)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    cli()

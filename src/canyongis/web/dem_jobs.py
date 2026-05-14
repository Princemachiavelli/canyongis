"""Registry of DEM-derived web jobs.

A ``DemJobSpec`` declares one DEM-derived processing job: a unique key, a
human-readable label, the request body schema, the rendered visualizations
it produces, and a runner that does the work. The web server exposes the
specs over ``GET /api/job-specs`` and dispatches by key on
``POST /api/jobs/{dem_job_id}/run/{job_key}``, so adding a new feature is
one ``register(DemJobSpec(...))`` call.

Runners receive: ``dem_path``, ``out_dir`` (already created), the validated
``req`` model, a ``progress_cb`` for status lines, and ``output_url`` for
turning output paths into web URLs. They return the result dict that is
merged into the job's ``result`` field.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..drainage_preview import export_drainage_previews
from ..preview_tiles import TileArchive
from ..ravine_compare import (
    export_curvature_preview,
    export_geomorphon_ravine_preview,
    export_hillshade_preview,
    export_tpi_ravine_preview,
)

ProgressCb = Callable[[str], None]
OutputUrlFn = Callable[[Path], str]
JobRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Visualization:
    """One rendered output panel produced by a DEM job."""

    key: str
    label: str
    url_field: str


@dataclass(frozen=True)
class DemJobSpec:
    """Declarative spec for a DEM-derived job."""

    key: str
    label: str
    description: str
    request_model: type[BaseModel]
    visualizations: tuple[Visualization, ...]
    runner: JobRunner
    available: bool = True
    unavailable_reason: str | None = None


_REGISTRY: list[DemJobSpec] = []


def _tile_payload(prefix: str, archive: TileArchive | None, output_url: OutputUrlFn) -> dict[str, Any]:
    if archive is None:
        return {}
    return {
        f"{prefix}_pmtiles_path": archive.pmtiles_path,
        f"{prefix}_pmtiles_url": output_url(Path(archive.pmtiles_path)),
        f"{prefix}_tile_bounds_wgs84": archive.bounds_wgs84,
        f"{prefix}_tile_min_zoom": archive.min_zoom,
        f"{prefix}_tile_max_zoom": archive.max_zoom,
        f"{prefix}_tile_format": archive.tile_format,
        f"{prefix}_tile_archive_format": archive.tile_archive_format,
    }


def register(spec: DemJobSpec) -> None:
    if any(s.key == spec.key for s in _REGISTRY):
        raise ValueError(f"duplicate DemJobSpec key: {spec.key}")
    _REGISTRY.append(spec)


def list_specs() -> list[DemJobSpec]:
    return list(_REGISTRY)


def get_spec(key: str) -> DemJobSpec | None:
    return next((s for s in _REGISTRY if s.key == key), None)


class HillshadeRequest(BaseModel):
    azimuths_deg: Sequence[float] = Field(default=(225.0, 270.0, 315.0, 360.0))
    altitude_deg: float = 45.0


def _run_hillshade(
    *,
    dem_path: str,
    out_dir: Path,
    req: HillshadeRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    art = export_hillshade_preview(
        dem_path,
        out_dir,
        azimuths_deg=tuple(req.azimuths_deg),
        altitude_deg=req.altitude_deg,
        log_callback=progress_cb,
    )
    return {
        "dem_path": dem_path,
        "out_dir": art.out_dir,
        "bounds_wgs84": art.bounds_wgs84,
        "hillshade_tif_path": art.tif_path,
        "panel_hillshade_url": output_url(Path(art.panel_path)),
        **_tile_payload("panel_hillshade", art.tile_archive, output_url),
        "meta": art.meta,
    }


class DrainageRequest(BaseModel):
    thresholds: Sequence[int] = Field(default=(500, 5000, 50000))
    fill_depressions: bool = True
    line_width_px: int = 1


def _run_drainage(
    *,
    dem_path: str,
    out_dir: Path,
    req: DrainageRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    progress_cb(f"drainage-preview → {out_dir} (thresholds={tuple(req.thresholds)})")
    result = export_drainage_previews(
        dem_path,
        out_dir,
        thresholds=tuple(req.thresholds),
        fill_depressions=req.fill_depressions,
        line_width_px=req.line_width_px,
    )
    return {
        "dem_path": dem_path,
        "out_dir": str(out_dir),
        "flow_accum_path": result.flow_accum_path,
        "flow_preview_url": output_url(Path(result.flow_preview_path)),
        "stream_preview_url": output_url(Path(result.stream_preview_path)),
        **_tile_payload("flow_preview", result.flow_tile_archive, output_url),
        **_tile_payload("stream_preview", result.stream_tile_archive, output_url),
        "thresholds": list(result.thresholds),
        "stream_pixels": result.stream_pixels,
    }


class GeomorphonRavineRequest(BaseModel):
    scales_m: Sequence[float] = Field(default=(30.0, 100.0))
    display_pct: tuple[float, float] = (2.0, 98.0)
    alpha_gamma: float = 0.7


def _run_geomorphon(
    *,
    dem_path: str,
    out_dir: Path,
    req: GeomorphonRavineRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    art = export_geomorphon_ravine_preview(
        dem_path,
        out_dir,
        scales_m=tuple(req.scales_m),
        display_pct=req.display_pct,
        alpha_gamma=req.alpha_gamma,
        log_callback=progress_cb,
    )
    return {
        "dem_path": dem_path,
        "out_dir": art.out_dir,
        "bounds_wgs84": art.bounds_wgs84,
        "a_geomorphon_path": art.tif_path,
        "panel_a_geomorphon_url": output_url(Path(art.panel_path)),
        **_tile_payload("panel_a_geomorphon", art.tile_archive, output_url),
        "meta": art.meta,
    }


class TpiRavineRequest(BaseModel):
    scales_m: Sequence[float] = Field(default=(5.0, 10.0, 30.0, 100.0))
    combine: str = "normalized_sum"
    smooth_m: float = 1.0
    norm_pct: float = 99.0
    display_pct: tuple[float, float] = (2.0, 98.0)
    alpha_gamma: float = 0.7


def _run_tpi(
    *,
    dem_path: str,
    out_dir: Path,
    req: TpiRavineRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    art = export_tpi_ravine_preview(
        dem_path,
        out_dir,
        scales_m=tuple(req.scales_m),
        combine=req.combine,
        smooth_m=req.smooth_m,
        norm_pct=req.norm_pct,
        display_pct=req.display_pct,
        alpha_gamma=req.alpha_gamma,
        log_callback=progress_cb,
    )
    return {
        "dem_path": dem_path,
        "out_dir": art.out_dir,
        "bounds_wgs84": art.bounds_wgs84,
        "b_tpi_path": art.tif_path,
        "panel_b_tpi_url": output_url(Path(art.panel_path)),
        **_tile_payload("panel_b_tpi", art.tile_archive, output_url),
        "meta": art.meta,
    }


class CurvatureRequest(BaseModel):
    smooth_m: float = 5.0
    display_pct: tuple[float, float] = (2.0, 98.0)
    alpha_gamma: float = 0.7


def _run_curvature(
    *,
    dem_path: str,
    out_dir: Path,
    req: CurvatureRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    art = export_curvature_preview(
        dem_path,
        out_dir,
        smooth_m=req.smooth_m,
        display_pct=req.display_pct,
        alpha_gamma=req.alpha_gamma,
        log_callback=progress_cb,
    )
    return {
        "dem_path": dem_path,
        "out_dir": art.out_dir,
        "bounds_wgs84": art.bounds_wgs84,
        "c_curvature_path": art.tif_path,
        "panel_c_curvature_url": output_url(Path(art.panel_path)),
        **_tile_payload("panel_c_curvature", art.tile_archive, output_url),
        "meta": art.meta,
    }


register(
    DemJobSpec(
        key="hillshade",
        label="Hillshade (multi-directional)",
        description=(
            "Pure-NumPy Horn-slope hillshade averaged across four sun azimuths "
            "(Mark 1992). Used as the grayscale backdrop for every other preview."
        ),
        request_model=HillshadeRequest,
        visualizations=(
            Visualization(key="hillshade", label="Hillshade", url_field="panel_hillshade_url"),
        ),
        runner=_run_hillshade,
    )
)


register(
    DemJobSpec(
        key="drainage",
        label="Flow / drainage network",
        description=(
            "Pysheds D8 flow accumulation and stream-network previews at one or "
            "more accumulation thresholds."
        ),
        request_model=DrainageRequest,
        visualizations=(
            Visualization(key="flow", label="Flow accumulation", url_field="flow_preview_url"),
            Visualization(key="stream", label="Stream network", url_field="stream_preview_url"),
        ),
        runner=_run_drainage,
    )
)


register(
    DemJobSpec(
        key="geomorphon-ravine",
        label="Geomorphon ravine score (r.geomorphon)",
        description=(
            "Multi-scale GRASS r.geomorphon hollow|valley membership count. "
            "Requires GRASS; one of the slower jobs."
        ),
        request_model=GeomorphonRavineRequest,
        visualizations=(
            Visualization(
                key="a_geomorphon",
                label="Geomorphon ravines",
                url_field="panel_a_geomorphon_url",
            ),
        ),
        runner=_run_geomorphon,
    )
)


register(
    DemJobSpec(
        key="tpi-ravine",
        label="Multi-scale TPI ravine score",
        description=(
            "Pure-NumPy multi-scale topographic position index combined as "
            "normalized sum (or min) — emphasizes concave terrain."
        ),
        request_model=TpiRavineRequest,
        visualizations=(
            Visualization(key="b_tpi", label="TPI ravines", url_field="panel_b_tpi_url"),
        ),
        runner=_run_tpi,
    )
)


register(
    DemJobSpec(
        key="curvature",
        label="Plan curvature (Zevenbergen-Thorne)",
        description=(
            "Zevenbergen-Thorne plan curvature on a lightly smoothed DEM, "
            "negated so concave terrain (valley axes) is positive."
        ),
        request_model=CurvatureRequest,
        visualizations=(
            Visualization(
                key="c_curvature", label="Plan curvature", url_field="panel_c_curvature_url"
            ),
        ),
        runner=_run_curvature,
    )
)


from . import rvt_jobs as _rvt_jobs  # noqa: E402,F401

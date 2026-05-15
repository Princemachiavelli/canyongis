from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import geopandas as gpd
from pydantic import BaseModel, Field

from ..aoi_mosaic import build_aoi_dem_mosaic
from ..ingest import (
    ROPEWIKI_PAGE_URL,
    _download_file,
    _find_kml_file_title,
    _get_kml_download_url,
    parse_ropewiki_ref,
)
from ..validate import parse_kml
from .dem_jobs import DemJobSpec, InputSpec, LayerSpec, OutputSpec, register


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


class BuildDemToolRequest(BaseModel):
    name: str = Field(default="aoi", min_length=1)
    bounds: Bounds
    buffer_m: float = 200.0
    max_products: int = 24


class RopewikiKmlImportRequest(BaseModel):
    source: str = Field(..., min_length=1)
    container_dem_job_id: str | None = None


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
    ctx: Any, container_dem_job_id: str | None, bounds: list[float]
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


def _kml_import_path(ctx: Any, job_id: str, name: str) -> Path:
    return ctx.output_root / "kml" / job_id / _safe_kml_name(name)


def build_kml_import_result(
    ctx: Any,
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


def _run_build_dem(*, ctx: Any, req: BuildDemToolRequest, progress_cb, **_: Any) -> dict[str, Any]:
    name = _safe_name(req.name)
    out_dir = ctx.output_root / "areas" / name
    dem_path = out_dir / "dem.tif"
    progress_cb(f"build-dem -> {dem_path} (buffer_m={req.buffer_m})")
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


def _run_ropewiki_kml_import(
    *, ctx: Any, req: RopewikiKmlImportRequest, progress_cb, job_id: str, **_: Any
) -> dict[str, Any]:
    source = req.source.strip()
    if not source:
        raise ValueError("source is required")
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
        ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_")) if page_title else source
    )
    result = build_kml_import_result(
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


register(
    DemJobSpec(
        key="build-dem",
        label="Build DEM mosaic",
        description="Download cached TNM 1m DEM products for an AOI and write a cropped mosaic.",
        request_model=BuildDemToolRequest,
        visualizations=(),
        runner=_run_build_dem,
        runner_kind="context",
        inputs=(
            InputSpec(
                key="bounds", label="AOI bounds", kind="aoi_bounds", required=True, default="user"
            ),
        ),
        outputs=(
            OutputSpec(
                key="dem",
                label="DEM raster",
                path_field="dem_path",
                role="dem",
                asset_kind="raster",
                url_field="dem_url",
            ),
        ),
        layers=(
            LayerSpec(
                key="dem_footprint",
                label="DEM footprint",
                kind="footprint",
                role="container",
                opacity=0.12,
            ),
        ),
    )
)


register(
    DemJobSpec(
        key="kml-import",
        label="Import Ropewiki KML",
        description="Import a canyon route KML from a Ropewiki page URL or title.",
        request_model=RopewikiKmlImportRequest,
        visualizations=(),
        runner=_run_ropewiki_kml_import,
        runner_kind="context",
        inputs=(),
        outputs=(
            OutputSpec(
                key="kml",
                label="Route KML",
                path_field="kml_path",
                role="route",
                asset_kind="vector",
                url_field="kml_url",
            ),
        ),
        layers=(
            LayerSpec(
                key="route",
                label="Route",
                kind="vector-kml",
                url_field="kml_url",
                role="route",
            ),
        ),
    )
)

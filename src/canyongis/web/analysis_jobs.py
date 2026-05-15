from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

import geopandas as gpd
import rasterio
from pydantic import BaseModel, Field
from rasterio.warp import transform_bounds

from ..kml_snap import snap_kml_to_raster
from ..ravine_spine import export_ravine_spines
from ..track_profile import _rappels_geojson, analyze_track_profile
from .dem_jobs import DemJobSpec, InputSpec, LayerSpec, OutputSpec, register
from .source_jobs import _gdf_to_geojson, _safe_kml_name


class RavineSpineRequest(BaseModel):
    tpi_job_id: str | None = None
    threshold_pct: float = 94.0
    hysteresis_ratio: float = 0.75
    branch_keep_ratio: float = 0.20
    max_path_slope: float = 0.30
    parent_kml_job_id: str | None = None


class KmlSnapRequest(BaseModel):
    kml_job_id: str | None = None
    dem_job_id: str | None = None
    compare_job_id: str | None = None
    target: Literal["dem", "tpi", "geomorphon", "curvature"] = "dem"
    search_radius_m: float = Field(default=30.0, gt=0)
    sample_step_m: float = Field(default=10.0, gt=0)
    high_threshold: float | None = None


class TrackProfileRequest(BaseModel):
    kml_job_id: str | None = None
    dem_job_id: str
    category: Literal["descent", "approach", "unclassified"] = "descent"
    track_index: int = Field(default=0, ge=0)
    sample_step_m: float = Field(default=5.0, gt=0)
    rappel_window_m: float = Field(default=10.0, gt=0)
    min_rappel_drop_m: float = Field(default=3.0, gt=0)
    min_rappel_slope: float = Field(default=0.45, gt=0)


def _raw_input_id(inputs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = inputs.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for id_key in ("job_id", "id"):
                raw = value.get(id_key)
                if isinstance(raw, str) and raw:
                    return raw
    return None


def _raster_bounds_wgs84(path: str | Path) -> tuple[float, float, float, float]:
    with rasterio.open(path) as src:
        bounds = src.bounds
        west, south, east, north = transform_bounds(
            src.crs, "EPSG:4326", bounds.left, bounds.bottom, bounds.right, bounds.top
        )
        return (float(west), float(south), float(east), float(north))


def _compare_raster_path(compare_job: Any, target: str) -> Path:
    if not compare_job.result:
        raise ValueError("ravine-compare job has no result")
    if target == "tpi":
        return Path(str(compare_job.result["b_tpi_path"]))
    direct_field = {"geomorphon": "a_geomorphon_path", "curvature": "c_curvature_path"}.get(target)
    if direct_field and compare_job.result.get(direct_field):
        return Path(str(compare_job.result[direct_field]))
    out_dir = Path(str(compare_job.result.get("out_dir", "")))
    if not out_dir:
        raise ValueError("ravine-compare job is missing output directory")
    if target == "geomorphon":
        return out_dir / "a_geomorphon.tif"
    if target == "curvature":
        return out_dir / "c_curvature.tif"
    raise ValueError(f"unsupported snap target: {target}")


def _compare_display_high(compare_job: Any, target: str) -> float | None:
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


def _parsed_kml_layer_result(kml_path: Path) -> dict[str, Any]:
    from ..validate import parse_kml

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


def _layer_from_kml(ctx: Any, *, key: str, label: str, path: Path) -> dict[str, Any]:
    parsed = _parsed_kml_layer_result(path)
    return {
        "id": key,
        "key": key,
        "label": label,
        "kind": "vector-kml",
        "role": "route",
        "source": {"type": "kml", "path": str(path), "url": ctx.output_url(path)},
        **parsed,
    }


def _kml_job_id(req_id: str | None, inputs: dict[str, Any]) -> str:
    kml_job_id = req_id or _raw_input_id(inputs, "kml", "kml_job", "kml_job_id")
    if not kml_job_id:
        raise ValueError("kml_job_id is required")
    return kml_job_id


def _tpi_job_id(req_id: str | None, inputs: dict[str, Any]) -> str:
    tpi_job_id = req_id or _raw_input_id(inputs, "score", "compare", "tpi", "tpi_job_id")
    if not tpi_job_id:
        raise ValueError("tpi_job_id is required")
    return tpi_job_id


def _run_kml_snap(*, ctx: Any, req: KmlSnapRequest, inputs: dict[str, Any], progress_cb, **_: Any):
    kml_job_id = _kml_job_id(req.kml_job_id, inputs)
    kml_job = ctx.job_or_404(kml_job_id)
    if kml_job.status != "succeeded" or not kml_job.result:
        raise ValueError("KML import job has not succeeded")
    kml_path = Path(str(kml_job.result["kml_path"]))

    dem_path: Path | None = None
    snap_mode = "min" if req.target == "dem" else "max"
    high_threshold = req.high_threshold
    if req.target == "dem":
        dem_job_id = req.dem_job_id or _raw_input_id(inputs, "dem", "dem_job", "dem_job_id")
        if not dem_job_id:
            raise ValueError("dem_job_id is required for DEM snapping")
        dem_job = ctx.job_or_404(dem_job_id)
        if dem_job.status != "succeeded" or not dem_job.result:
            raise ValueError("DEM job has not succeeded")
        dem_path = Path(str(dem_job.result["dem_path"]))
        raster_path = dem_path
    else:
        compare_job_id = req.compare_job_id or _raw_input_id(
            inputs, "score", "compare", "compare_job_id"
        )
        if not compare_job_id:
            raise ValueError("compare_job_id is required for score-raster snapping")
        compare_job = ctx.job_or_404(compare_job_id)
        if compare_job.status != "succeeded" or not compare_job.result:
            raise ValueError("compare job has not succeeded")
        dem_path = Path(str(compare_job.result["dem_path"]))
        raster_path = _compare_raster_path(compare_job, req.target)
        if high_threshold is None:
            high_threshold = _compare_display_high(compare_job, req.target)
    if not raster_path.exists():
        raise ValueError(f"snap raster does not exist: {raster_path}")

    out_dir = ctx.output_root / "jobs" / f"kml-snap-{ctx.now().replace(':', '').replace('.', '')}"
    kml_name = Path(str(kml_job.result.get("name") or kml_path.name)).stem
    name = f"{kml_name}-{req.target}-snapped.kml"
    progress_cb(
        f"kml-snap -> {out_dir} (target={req.target}, radius={req.search_radius_m:g}m, "
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
    line_gdf = cast(
        gpd.GeoDataFrame,
        snapped_gdf.loc[geom_type.isin(["LineString", "MultiLineString"])].reset_index(drop=True),
    )
    point_gdf = cast(
        gpd.GeoDataFrame,
        snapped_gdf.loc[geom_type.isin(["Point", "MultiPoint"])].reset_index(drop=True),
    )
    category = cast(Any, line_gdf["category"]) if "category" in line_gdf.columns else None
    descent = (
        cast(gpd.GeoDataFrame, line_gdf.loc[category == "descent"].reset_index(drop=True))
        if category is not None
        else line_gdf.iloc[0:0]
    )
    approach = (
        cast(gpd.GeoDataFrame, line_gdf.loc[category == "approach"].reset_index(drop=True))
        if category is not None
        else line_gdf.iloc[0:0]
    )
    unclassified = (
        cast(
            gpd.GeoDataFrame,
            line_gdf.loc[~category.isin(["descent", "approach"])].reset_index(drop=True),
        )
        if category is not None
        else line_gdf
    )
    bounds = _raster_bounds_wgs84(dem_path) if dem_path else _raster_bounds_wgs84(raster_path)
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


def _run_track_profile(
    *, ctx: Any, req: TrackProfileRequest, inputs: dict[str, Any], progress_cb, **_: Any
):
    kml_job_id = _kml_job_id(req.kml_job_id, inputs)
    kml_job = ctx.job_or_404(kml_job_id)
    if kml_job.status != "succeeded" or not kml_job.result:
        raise ValueError("KML job has not succeeded")
    dem_job = ctx.job_or_404(req.dem_job_id)
    if dem_job.status != "succeeded" or not dem_job.result:
        raise ValueError("DEM job has not succeeded")
    kml_path = Path(str(kml_job.result["kml_path"]))
    dem_path = Path(str(dem_job.result["dem_path"]))
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
    payload["layers"] = [
        {
            "id": "rappel_candidates",
            "key": "rappel_candidates",
            "label": "Rappel candidates",
            "kind": "point-events",
            "role": "rappel-candidates",
            "source": {"type": "geojson", "field": "rappel_candidates_geojson"},
            "geojson": payload["rappel_candidates_geojson"],
        }
    ]
    return payload


def _run_ravine_spine(
    *, ctx: Any, req: RavineSpineRequest, inputs: dict[str, Any], progress_cb, **_: Any
):
    tpi_job_id = _tpi_job_id(req.tpi_job_id, inputs)
    tpi_job = ctx.job_or_404(tpi_job_id)
    if tpi_job.status != "succeeded" or not tpi_job.result:
        raise ValueError("TPI job has not succeeded")
    if "b_tpi_path" not in tpi_job.result or "dem_path" not in tpi_job.result:
        raise ValueError("TPI job missing b_tpi_path/dem_path")
    if req.parent_kml_job_id:
        parent_kml_job = ctx.job_or_404(req.parent_kml_job_id)
        if parent_kml_job.status != "succeeded" or not parent_kml_job.result:
            raise ValueError("parent KML job has not succeeded")

    out_dir = (
        ctx.output_root / "jobs" / f"ravine-spine-{ctx.now().replace(':', '').replace('.', '')}"
    )
    progress_cb(f"ravine-spine -> {out_dir}")
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
    payload = {
        **asdict(result),
        "bounds_wgs84": tpi_job.result.get("bounds_wgs84"),
        "tpi_job_id": tpi_job_id,
        "parent_kml_job_id": req.parent_kml_job_id,
        "rappel_candidates_path": str(out_dir / "rappel_candidates.kml"),
    }
    spines_path = Path(payload["spines_path"])
    rappels_path = Path(payload["rappel_candidates_path"])
    payload["spines_kml_url"] = ctx.output_url(spines_path) if spines_path.exists() else None
    payload["rappel_candidates_kml_url"] = (
        ctx.output_url(rappels_path) if rappels_path.exists() else None
    )
    layers = []
    if spines_path.exists():
        layers.append(
            _layer_from_kml(ctx, key="spines_layer", label="Ravine spines", path=spines_path)
        )
    if rappels_path.exists():
        layers.append(
            _layer_from_kml(
                ctx, key="rappel_candidates_layer", label="Rappel candidates", path=rappels_path
            )
        )
    payload["layers"] = layers
    return payload


register(
    DemJobSpec(
        key="kml-snap",
        label="Snap KML to terrain",
        description="Snap KML route geometry to nearby DEM lows or ravine-score highs.",
        request_model=KmlSnapRequest,
        visualizations=(),
        runner=_run_kml_snap,
        runner_kind="context",
        inputs=(
            InputSpec(key="kml", label="KML route", kind="vector.kml"),
            InputSpec(key="dem", label="DEM", kind="raster.dem", required=False),
            InputSpec(key="score", label="Score raster", kind="raster.score", required=False),
        ),
        outputs=(
            OutputSpec("kml", "Snapped KML", "kml_path", "route", "vector", "kml_url"),
            OutputSpec("gpkg", "Snapped GeoPackage", "snapped_gpkg_path", "route", "vector"),
        ),
        layers=(LayerSpec("route", "Snapped route", "vector-kml", "kml_url", "route"),),
    )
)


register(
    DemJobSpec(
        key="track-profile",
        label="Track elevation profile",
        description="Sample a KML track against a DEM and flag steep rappel-like drops.",
        request_model=TrackProfileRequest,
        visualizations=(),
        runner=_run_track_profile,
        runner_kind="context",
        inputs=(
            InputSpec(key="kml", label="KML route", kind="vector.kml"),
            InputSpec(key="dem", label="DEM", kind="raster.dem"),
        ),
        outputs=(),
        layers=(),
    )
)


register(
    DemJobSpec(
        key="ravine-spine",
        label="Ravine spine from TPI",
        description="Extract ravine centerline spines and rappel candidates from a ravine-score raster.",
        request_model=RavineSpineRequest,
        visualizations=(),
        runner=_run_ravine_spine,
        runner_kind="context",
        inputs=(
            InputSpec(key="score", label="Ravine score raster", kind="raster.score"),
            InputSpec(key="dem", label="DEM", kind="raster.dem"),
        ),
        outputs=(
            OutputSpec(
                "spines", "Ravine spines KML", "spines_path", "route", "vector", "spines_kml_url"
            ),
            OutputSpec(
                "rappel_candidates",
                "Rappel candidates KML",
                "rappel_candidates_path",
                "rappel-candidates",
                "vector",
                "rappel_candidates_kml_url",
            ),
        ),
        layers=(),
    )
)

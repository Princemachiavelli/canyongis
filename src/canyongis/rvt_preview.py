"""Wrapper around the Relief Visualization Toolbox (rvt-py, Kokalj et al.).

Produces per-visualization GeoTIFFs and PNG panels overlaid on the
canyongis multi-directional hillshade backdrop. Each visualization is
wrapped in a defensive try/except so a missing or broken individual
function doesn't take down the whole job.

See https://rvt-py.readthedocs.io/en/latest/listofvis_main.html.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image

import rvt.blend
import rvt.blend_func
import rvt.vis

from .ravine_compare import (
    OVERLAY_DOWNSAMPLE_FACTOR,
    PreviewArtifacts,
    _downsample_by_factor,
    _make_emit,
    _open_dem,
    _render_score_panel,
    _save_panel_webp,
    _warp_rgb_image_to_wgs84,
    _write_score_geotiff,
    compute_hillshade,
)

RVT_VIS_KEYS: tuple[str, ...] = (
    "slope",
    "svf",
    "asvf",
    "opns_pos",
    "opns_neg",
    "sky_illum",
    "local_dominance",
    "msrm",
    "mstp",
    "slrm",
    "vat",
)

RVT_VIS_LABELS: dict[str, str] = {
    "slope": "Slope (RVT)",
    "svf": "Sky-view factor",
    "asvf": "Anisotropic SVF",
    "opns_pos": "Positive openness",
    "opns_neg": "Negative openness",
    "sky_illum": "Sky illumination",
    "local_dominance": "Local dominance",
    "msrm": "Multi-scale relief (MSRM)",
    "mstp": "Enhanced multi-scale topographic position (e4MSTP)",
    "slrm": "Simple local relief (SLRM)",
    "vat": "VAT (combined)",
}


def _save_rgb_panel(
    rgb: np.ndarray, transform, crs, out_path: Path
) -> tuple[float, float, float, float]:
    arr = rgb
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[2] not in (3, 4):
        arr = np.transpose(arr, (1, 2, 0))
    if arr.dtype != np.uint8:
        arr = np.clip(arr * (255.0 if arr.max() <= 1.0 else 1.0), 0, 255).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    overlay, bounds = _warp_rgb_image_to_wgs84(
        Image.fromarray(arr, mode="RGB"), src_transform=transform, src_crs=crs
    )
    _downsample_by_factor(overlay, OVERLAY_DOWNSAMPLE_FACTOR).save(
        out_path, quality=85, method=4
    )
    return bounds


def export_rvt_previews(
    dem_path: str | Path,
    out_dir: str | Path,
    *,
    visualizations: tuple[str, ...] = RVT_VIS_KEYS,
    display_pct: tuple[float, float] = (2.0, 98.0),
    alpha_gamma: float = 0.7,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, PreviewArtifacts]:
    dem_path = Path(dem_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    emit = _make_emit(log_callback)
    requested = tuple(v for v in visualizations if v in RVT_VIS_KEYS)
    emit(f"rvt: {dem_path} → {out_dir} ({len(requested)} vis: {','.join(requested)})")

    elev, transform, crs, pixel_size, profile = _open_dem(dem_path)
    hillshade = compute_hillshade(elev, pixel_size)
    filled = np.where(np.isfinite(elev), elev, float(np.nanmedian(elev))).astype(np.float32)

    artifacts: dict[str, PreviewArtifacts] = {}

    def write_score(
        key: str, score: np.ndarray, *, invert_for_display: bool = False
    ) -> None:
        tif_path = out_dir / f"rvt_{key}.tif"
        _write_score_geotiff(tif_path, score, profile)
        display = -score if invert_for_display else score
        panel, lo, hi = _render_score_panel(
            display, hillshade, display_pct=display_pct, alpha_gamma=alpha_gamma
        )
        panel_path = out_dir / f"panel_rvt_{key}.webp"
        bounds = _save_panel_webp(panel, transform, crs, panel_path)
        artifacts[key] = PreviewArtifacts(
            out_dir=str(out_dir),
            bounds_wgs84=bounds,
            panel_path=str(panel_path),
            tif_path=str(tif_path),
            meta={"display_lo": lo, "display_hi": hi},
        )

    def write_rgb(key: str, rgb: np.ndarray) -> None:
        panel_path = out_dir / f"panel_rvt_{key}.webp"
        bounds = _save_rgb_panel(rgb, transform, crs, panel_path)
        artifacts[key] = PreviewArtifacts(
            out_dir=str(out_dir),
            bounds_wgs84=bounds,
            panel_path=str(panel_path),
            tif_path=str(panel_path),
            meta={},
        )

    def attempt(key: str, fn: Callable[[], None]) -> None:
        if key not in requested:
            return
        try:
            emit(f"rvt: {key}")
            fn()
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            emit(f"rvt: skipped {key}: {exc}")

    def _slope() -> None:
        out = rvt.vis.slope_aspect(
            dem=filled,
            resolution_x=pixel_size,
            resolution_y=pixel_size,
            output_units="degree",
            ve_factor=1,
        )
        write_score("slope", np.asarray(out["slope"], dtype=np.float32))

    def _svf_family() -> None:
        out = rvt.vis.sky_view_factor(
            dem=filled,
            resolution=pixel_size,
            compute_svf="svf" in requested,
            compute_asvf="asvf" in requested,
            compute_opns="opns_pos" in requested,
        )
        if "svf" in requested and "svf" in out:
            write_score("svf", np.asarray(out["svf"], dtype=np.float32), invert_for_display=True)
        if "asvf" in requested and "asvf" in out:
            write_score(
                "asvf", np.asarray(out["asvf"], dtype=np.float32), invert_for_display=True
            )
        if "opns_pos" in requested and "opns" in out:
            write_score(
                "opns_pos",
                np.asarray(out["opns"], dtype=np.float32),
                invert_for_display=True,
            )

    def _neg_openness() -> None:
        out = rvt.vis.sky_view_factor(
            dem=-filled,
            resolution=pixel_size,
            compute_svf=False,
            compute_asvf=False,
            compute_opns=True,
        )
        write_score("opns_neg", np.asarray(out["opns"], dtype=np.float32))

    def _sky_illum() -> None:
        score = rvt.vis.sky_illumination(
            dem=filled,
            resolution=pixel_size,
            sky_model="overcast",
            compute_shadow=False,
        )
        write_score("sky_illum", np.asarray(score, dtype=np.float32), invert_for_display=True)

    def _local_dominance() -> None:
        score = rvt.vis.local_dominance(dem=filled, min_rad=10, max_rad=20)
        write_score("local_dominance", np.asarray(score, dtype=np.float32))

    def _msrm() -> None:
        score = rvt.vis.msrm(
            dem=filled,
            resolution=pixel_size,
            feature_min=1.0,
            feature_max=5.0,
            scaling_factor=3,
        )
        write_score("msrm", np.asarray(score, dtype=np.float32))

    def _mstp() -> None:
        rgb = rvt.blend.e4mstp(dem=filled, resolution=pixel_size)
        write_rgb("mstp", np.asarray(rgb))

    def _slrm() -> None:
        score = rvt.vis.slrm(dem=filled, radius_cell=20)
        write_score("slrm", np.asarray(score, dtype=np.float32))

    def _vat() -> None:
        rgb = rvt.blend_func.vat(dem=filled, resolution=pixel_size)
        write_rgb("vat", np.asarray(rgb))

    attempt("slope", _slope)
    if {"svf", "asvf", "opns_pos"} & set(requested):
        attempt("svf-family", _svf_family)
    attempt("opns_neg", _neg_openness)
    attempt("sky_illum", _sky_illum)
    attempt("local_dominance", _local_dominance)
    attempt("msrm", _msrm)
    attempt("mstp", _mstp)
    attempt("slrm", _slrm)
    attempt("vat", _vat)

    return artifacts

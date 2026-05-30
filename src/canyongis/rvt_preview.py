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
import rvt.blend
import rvt.vis
from PIL import Image

from .ravine_compare import (
    PreviewArtifacts,
    _make_emit,
    _open_dem,
    _render_rgba_panel,
    _save_panel_previews,
    _write_score_geotiff,
)

# Slope clamp ceiling for the VAT blend. RVT's published default is 50°, which
# is tuned for archaeological / agricultural terrain. Canyon walls regularly
# exceed 50° (and saturate to a single colour), which wipes out detail in the
# lower / steeper half of canyon maps. 70° covers most real canyon slopes and
# only the vertical-cliff pixels saturate.
VAT_SLOPE_MAX_DEG = 70.0

RVT_VIS_KEYS: tuple[str, ...] = (
    "vat",
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
)

RVT_VIS_LABELS: dict[str, str] = {
    "vat": "VAT (archaeological topography)",
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
}

# Base-layer keys: these render as a complete grayscale/RGB composite suitable
# for the map's base layer slot (not as a translucent overlay).
RVT_BASE_LAYER_KEYS: frozenset[str] = frozenset({"vat"})


def _save_rgb_panel(rgb: np.ndarray, valid_mask: np.ndarray, transform, crs, out_path: Path):
    arr = rgb
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[2] not in (3, 4):
        arr = np.transpose(arr, (1, 2, 0))
    if arr.dtype != np.uint8:
        arr = np.clip(arr * (255.0 if arr.max() <= 1.0 else 1.0), 0, 255).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    return _save_panel_previews(
        Image.fromarray(arr, mode="RGB"), valid_mask, transform, crs, out_path
    )


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
    valid_mask = np.isfinite(elev)
    filled = np.where(np.isfinite(elev), elev, float(np.nanmedian(elev))).astype(np.float32)

    artifacts: dict[str, PreviewArtifacts] = {}

    def write_score(key: str, score: np.ndarray, *, invert_for_display: bool = False) -> None:
        tif_path = out_dir / f"rvt_{key}.tif"
        _write_score_geotiff(tif_path, score, profile)
        display = -score if invert_for_display else score
        panel, lo, hi = _render_rgba_panel(
            display, display_pct=display_pct, alpha_gamma=alpha_gamma
        )
        panel_path = out_dir / f"panel_rvt_{key}.webp"
        bounds, tile_archive = _save_panel_previews(panel, valid_mask, transform, crs, panel_path)
        artifacts[key] = PreviewArtifacts(
            out_dir=str(out_dir),
            bounds_wgs84=bounds,
            panel_path=str(panel_path),
            tif_path=str(tif_path),
            meta={"display_lo": lo, "display_hi": hi},
            tile_archive=tile_archive,
        )

    def write_rgb(key: str, rgb: np.ndarray) -> None:
        panel_path = out_dir / f"panel_rvt_{key}.webp"
        bounds, tile_archive = _save_rgb_panel(rgb, valid_mask, transform, crs, panel_path)
        artifacts[key] = PreviewArtifacts(
            out_dir=str(out_dir),
            bounds_wgs84=bounds,
            panel_path=str(panel_path),
            tif_path=str(panel_path),
            meta={},
            tile_archive=tile_archive,
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
            write_score("asvf", np.asarray(out["asvf"], dtype=np.float32), invert_for_display=True)
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
        # VAT - Archaeological combination (Kokalj et al.): the four-layer blend
        # that ships as RVT's reference archaeological visualization. Layer
        # parameters mirror rvt.blend.create_blender_file_example, except the
        # Slope gradient ceiling is widened (see VAT_SLOPE_MAX_DEG) to keep
        # detail on steep canyon walls.
        comb = rvt.blend.BlenderCombination()
        comb.add_dem_arr(filled, pixel_size)
        comb.create_layer(
            vis_method="Sky-View Factor",
            normalization="value",
            minimum=0.7,
            maximum=1.0,
            blend_mode="multiply",
            opacity=25,
        )
        comb.create_layer(
            vis_method="Openness - Positive",
            normalization="value",
            minimum=68,
            maximum=93,
            blend_mode="overlay",
            opacity=50,
        )
        comb.create_layer(
            vis_method="Slope gradient",
            normalization="value",
            minimum=0,
            maximum=VAT_SLOPE_MAX_DEG,
            blend_mode="luminosity",
            opacity=50,
        )
        comb.create_layer(
            vis_method="Hillshade",
            normalization="value",
            minimum=0,
            maximum=1,
            blend_mode="normal",
            opacity=100,
        )
        rendered = np.asarray(
            comb.render_all_images(save_visualizations=False, save_float=True)
        )

        # render_all_images returns either a 2D luminosity array or a 3D
        # (channels-first / channels-last) RGB array depending on which blend
        # modes were active. We want the panel to be saved as raw grayscale /
        # RGB — *not* fed through _render_rgba_panel which applies a viridis
        # colormap and percentile stretch (write_score does that).
        if rendered.ndim == 3:
            rgb = rendered
            if rgb.shape[0] in (3, 4) and rgb.shape[2] not in (3, 4):
                rgb = np.transpose(rgb, (1, 2, 0))
            if rgb.shape[2] == 4:
                rgb = rgb[..., :3]
        elif rendered.ndim == 2:
            rgb = np.repeat(rendered[..., None], 3, axis=2)
        else:
            raise ValueError(f"unexpected VAT render shape: {rendered.shape}")

        if rgb.dtype != np.uint8:
            scale = 255.0 if float(np.nanmax(rgb)) <= 1.0 + 1e-6 else 1.0
            rgb_u8 = np.clip(rgb.astype(np.float32) * scale, 0, 255).astype(np.uint8)
        else:
            rgb_u8 = rgb

        tif_path = out_dir / "rvt_vat.tif"
        if rendered.ndim == 2:
            _write_score_geotiff(tif_path, np.asarray(rendered, dtype=np.float32), profile)
        else:
            # No useful single-band score for an RGB blend; mirror the panel
            # path so downstream consumers still have a valid file reference.
            tif_path = out_dir / "panel_rvt_vat.webp"

        panel = Image.fromarray(rgb_u8, mode="RGB")
        panel_path = out_dir / "panel_rvt_vat.webp"
        bounds, tile_archive = _save_panel_previews(
            panel, valid_mask, transform, crs, panel_path
        )
        artifacts["vat"] = PreviewArtifacts(
            out_dir=str(out_dir),
            bounds_wgs84=bounds,
            panel_path=str(panel_path),
            tif_path=str(tif_path),
            meta={
                "vat_slope_max_deg": VAT_SLOPE_MAX_DEG,
                "vat_archaeological": True,
            },
            tile_archive=tile_archive,
        )

    attempt("vat", _vat)
    attempt("slope", _slope)
    if {"svf", "asvf", "opns_pos"} & set(requested):
        attempt("svf-family", _svf_family)
    attempt("opns_neg", _neg_openness)
    attempt("sky_illum", _sky_illum)
    attempt("local_dominance", _local_dominance)
    attempt("msrm", _msrm)
    attempt("mstp", _mstp)
    attempt("slrm", _slrm)

    return artifacts

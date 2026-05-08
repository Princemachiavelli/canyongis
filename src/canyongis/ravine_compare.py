"""Compute and visualize three label-free ravine-network signals on a DEM.

A. Geomorphon hollow|valley membership at one or more search radii.
B. Multi-scale TPI (sum of clipped negative TPI across radii).
C. Plan curvature (Zevenbergen-Thorne) on a lightly smoothed DEM.

Writes one GeoTIFF per signal plus colored PNG overlays on a hillshade backdrop
so the three approaches can be visually compared on the same DEM.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.features import rasterize
from scipy import ndimage as ndi

from .geomorphons import export_geomorphons
from .io import log, read_dem
from .terrain import fill_nans, meters_to_px
from .validate import parse_kml


@dataclass(frozen=True)
class RavineCompareResult:
    dem_path: str
    out_dir: str
    geomorphon_path: str
    tpi_path: str
    curvature_path: str
    hillshade_path: str
    compare_png_path: str


_RAMP = np.array(
    [
        (68, 1, 84),
        (72, 40, 120),
        (62, 73, 137),
        (49, 104, 142),
        (38, 130, 142),
        (31, 158, 137),
        (53, 183, 121),
        (109, 205, 89),
        (180, 222, 44),
        (253, 231, 37),
    ],
    dtype=np.float32,
)


def _parse_scales(value: str) -> tuple[float, ...]:
    parts = tuple(float(part) for part in value.replace(" ", "").split(",") if part)
    if not parts:
        raise argparse.ArgumentTypeError("expected at least one scale in meters")
    if any(s <= 0 for s in parts):
        raise argparse.ArgumentTypeError("scales must be positive")
    return parts


def compute_hillshade(
    elev: np.ndarray,
    pixel_size: float,
    *,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
    z_factor: float = 1.0,
) -> np.ndarray:
    """8-bit hillshade matching GDAL defaults (Horn slope, single light)."""
    filled = fill_nans(elev)
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32) / (8 * pixel_size)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32) / (8 * pixel_size)
    dzdx = ndi.convolve(filled * z_factor, kx, mode="nearest")
    dzdy = ndi.convolve(filled * z_factor, ky, mode="nearest")
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    az = np.deg2rad(360.0 - azimuth_deg + 90.0)
    zen = np.deg2rad(90.0 - altitude_deg)
    cos_inc = np.cos(zen) * np.cos(slope) + np.sin(zen) * np.sin(slope) * np.cos(az - aspect)
    shade = np.clip(cos_inc, 0.0, 1.0)
    out = (shade * 255.0).astype(np.uint8)
    out[np.isnan(elev)] = 0
    return out


def compute_a_geomorphon(
    dem_path: str | Path,
    scales_m: tuple[float, ...],
    work_dir: Path,
) -> tuple[np.ndarray, dict]:
    """Multi-scale geomorphon ravine score: count of scales classifying a pixel
    as hollow (form 7) or valley (form 9). Returns float32 [0, len(scales)]."""
    work_dir.mkdir(parents=True, exist_ok=True)
    score = None
    forms_paths: list[str] = []
    for scale_m in scales_m:
        sub = work_dir / f"geomorphon_{int(round(scale_m))}m"
        sub.mkdir(parents=True, exist_ok=True)
        export_geomorphons(
            dem_path,
            sub,
            search_m=scale_m,
            outputs=("forms",),
            extended=False,
            quiet=True,
        )
        forms_path = sub / "geomorphon_forms.tif"
        with rasterio.open(forms_path) as src:
            forms = src.read(1)
        is_ravine = ((forms == 7) | (forms == 9)).astype(np.float32)
        score = is_ravine if score is None else score + is_ravine
        forms_paths.append(str(forms_path))
        log(f"[A] geomorphon @ {scale_m}m: ravine_pixels={int(is_ravine.sum())}")
    if score is None:
        raise ValueError("geomorphon scales must be non-empty")
    meta = {"scales_m": list(scales_m), "form_rasters": forms_paths}
    return score, meta


def compute_b_tpi(
    elev: np.ndarray,
    pixel_size: float,
    scales_m: tuple[float, ...],
    *,
    combine: str = "normalized_sum",
    smooth_m: float = 1.0,
    norm_pct: float = 99.0,
) -> tuple[np.ndarray, dict]:
    """Multi-scale TPI ravine score across radii.

    TPI < 0 means below local mean elevation (concave terrain). For each
    radius we compute `neg_r = max(0, -TPI_r)`. The per-scale neg arrays are
    then combined according to ``combine``:

    - ``"sum"`` (default, legacy): plain additive sum. Wide-radius terms tend
      to dominate because their TPI magnitudes are larger.
    - ``"normalized_sum"``: each scale is divided by its ``norm_pct``-percentile
      before summing, so every scale contributes ~equally — narrow features
      survive alongside broad valleys.
    - ``"min"``: per-pixel minimum of normalized neg values. Only fires where
      the pixel is concave at *every* scale (slot/ravine that's also in a
      wider valley). Output is rescaled by ``len(scales_m)`` so display
      stretching behaves like the sum modes.

    ``smooth_m`` applies a Gaussian pre-smooth to the DEM (in meters); useful
    at very small radii on noisy 1 m DEMs.

    Computed on the NaN-filled DEM so the focal-mean stays defined near nodata
    edges; the final score is masked back to NaN at original-nodata pixels.
    """
    if combine not in {"sum", "normalized_sum", "min"}:
        raise ValueError(f"unknown combine mode: {combine!r}")
    filled = fill_nans(elev)
    if smooth_m > 0:
        sigma_px = smooth_m / max(pixel_size, 1e-6)
        filled = ndi.gaussian_filter(filled, sigma=sigma_px, mode="nearest")

    per_scale: list[np.ndarray] = []
    per_scale_norms: list[float] = []
    for radius_m in scales_m:
        r_px = meters_to_px(radius_m, pixel_size)
        focal_mean = ndi.uniform_filter(filled, size=2 * r_px + 1, mode="nearest")
        tpi = (filled - focal_mean).astype(np.float32)
        neg = np.maximum(-tpi, 0.0)
        per_scale.append(neg)
        finite = neg[np.isfinite(neg)]
        norm = float(np.percentile(finite, norm_pct)) if finite.size else 1.0
        per_scale_norms.append(max(norm, 1e-6))
        log(f"[B] tpi @ {radius_m}m: mean_neg={float(neg.mean()):.3f} p{norm_pct:g}={norm:.3f}")

    if combine == "sum":
        score = np.sum(per_scale, axis=0).astype(np.float32)
    else:
        normed = [
            np.clip(s / n, 0.0, 1.0).astype(np.float32)
            for s, n in zip(per_scale, per_scale_norms, strict=True)
        ]
        if combine == "normalized_sum":
            score = np.sum(normed, axis=0).astype(np.float32)
        else:  # "min"
            score = (np.min(np.stack(normed, axis=0), axis=0) * len(scales_m)).astype(np.float32)
    score[np.isnan(elev)] = np.nan
    meta = {
        "scales_m": list(scales_m),
        "combine": combine,
        "smooth_m": smooth_m,
        "norm_pct": norm_pct,
        "per_scale_norms": per_scale_norms,
    }
    return score, meta


def compute_c_plan_curvature(
    elev: np.ndarray,
    pixel_size: float,
    smooth_m: float,
) -> tuple[np.ndarray, dict]:
    """Zevenbergen-Thorne plan curvature on a Gaussian-smoothed DEM.

    Returned values are negated so positive = converging (valley/ravine axis),
    matching the orientation of A and B.
    """
    filled = fill_nans(elev)
    sigma_px = max(smooth_m / max(pixel_size, 1e-6), 0.5)
    z = ndi.gaussian_filter(filled, sigma=sigma_px, mode="nearest")
    L = pixel_size
    p = (np.roll(z, -1, axis=1) - np.roll(z, 1, axis=1)) / (2 * L)
    q = (np.roll(z, 1, axis=0) - np.roll(z, -1, axis=0)) / (2 * L)
    r = (np.roll(z, -1, axis=1) - 2 * z + np.roll(z, 1, axis=1)) / (L * L)
    t = (np.roll(z, 1, axis=0) - 2 * z + np.roll(z, -1, axis=0)) / (L * L)
    s = (
        np.roll(np.roll(z, -1, axis=1), 1, axis=0)
        - np.roll(np.roll(z, 1, axis=1), 1, axis=0)
        - np.roll(np.roll(z, -1, axis=1), -1, axis=0)
        + np.roll(np.roll(z, 1, axis=1), -1, axis=0)
    ) / (4 * L * L)
    pp_qq = p * p + q * q
    denom = pp_qq * np.power(1.0 + pp_qq, 1.5) + 1e-9
    k_plan = (q * q * r - 2 * p * q * s + p * p * t) / denom
    score = (-k_plan).astype(np.float32)
    score[np.isnan(elev)] = np.nan
    return score, {"smooth_m": smooth_m, "sigma_px": float(sigma_px)}


def _percentile_stretch(
    arr: np.ndarray,
    lo_pct: float,
    hi_pct: float,
) -> tuple[np.ndarray, float, float]:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.float32), 0.0, 1.0
    lo = float(np.percentile(finite, lo_pct))
    hi = float(np.percentile(finite, hi_pct))
    if hi <= lo:
        hi = lo + 1e-6
    out = np.clip((np.where(np.isfinite(arr), arr, lo) - lo) / (hi - lo), 0.0, 1.0).astype(
        np.float32
    )
    return out, lo, hi


def _colorize_score(score01: np.ndarray) -> np.ndarray:
    pos = score01 * (len(_RAMP) - 1)
    lo = np.floor(pos).astype(np.int16)
    hi = np.clip(lo + 1, 0, len(_RAMP) - 1)
    frac = (pos - lo)[..., None]
    rgb = _RAMP[lo] * (1.0 - frac) + _RAMP[hi] * frac
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _blend_over_hillshade(
    hillshade_u8: np.ndarray,
    score01: np.ndarray,
    *,
    alpha_gamma: float = 0.7,
) -> Image.Image:
    backdrop = np.repeat(hillshade_u8[..., None], 3, axis=2).astype(np.float32)
    rgb = _colorize_score(score01).astype(np.float32)
    alpha = np.power(np.clip(score01, 0.0, 1.0), alpha_gamma)[..., None]
    blended = backdrop * (1.0 - alpha) + rgb * alpha
    return Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8), mode="RGB")


def _overlay_kml_mask(
    image: Image.Image,
    mask: np.ndarray,
    *,
    color: tuple[int, int, int] = (255, 0, 255),
    width_px: int = 5,
    opacity: float = 1.0,
) -> None:
    """Alpha-blend the descent track over the image as a filled line of given
    width. opacity=1.0 paints fully; smaller values let the backdrop show through."""
    if mask.shape[0] != image.height or mask.shape[1] != image.width:
        return
    iters = max(int(width_px) // 2, 0)
    line_mask = (
        ndi.binary_dilation(mask.astype(bool), iterations=iters) if iters > 0 else mask.astype(bool)
    )
    if not line_mask.any():
        return
    rgb = np.asarray(image).copy().astype(np.float32)
    color_arr = np.array(color, dtype=np.float32)
    alpha = float(np.clip(opacity, 0.0, 1.0))
    rgb[line_mask] = rgb[line_mask] * (1.0 - alpha) + color_arr * alpha
    image.paste(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB"))


def _font(size: int = 22) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _label_panel(panel: Image.Image, title: str) -> Image.Image:
    title_h = 38
    out = Image.new("RGB", (panel.width, panel.height + title_h), "white")
    out.paste(panel, (0, title_h))
    draw = ImageDraw.Draw(out)
    draw.text((10, 6), title, fill=(0, 0, 0), font=_font(20))
    return out


def _downsample(image: Image.Image, max_px: int) -> Image.Image:
    if max(image.size) <= max_px:
        return image
    scale = max_px / max(image.size)
    new_size = (max(1, round(image.size[0] * scale)), max(1, round(image.size[1] * scale)))
    return image.resize(new_size, Image.LANCZOS)


def _grid_2x2(panels: list[Image.Image]) -> Image.Image:
    assert len(panels) == 4
    gap = 16
    w = max(p.width for p in panels)
    h = max(p.height for p in panels)
    out = Image.new("RGB", (2 * w + gap, 2 * h + gap), "white")
    out.paste(panels[0], (0, 0))
    out.paste(panels[1], (w + gap, 0))
    out.paste(panels[2], (0, h + gap))
    out.paste(panels[3], (w + gap, h + gap))
    return out


def _write_score_geotiff(
    path: Path,
    arr: np.ndarray,
    profile: dict,
) -> None:
    nodata = -9999.0
    out = np.where(np.isfinite(arr), arr, nodata).astype(np.float32)
    out_profile = profile.copy()
    out_profile.update(
        dtype="float32",
        nodata=nodata,
        count=1,
        compress="deflate",
        predictor=3,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **out_profile) as dst:
        dst.write(out, 1)


def _write_hillshade_geotiff(path: Path, hillshade_u8: np.ndarray, profile: dict) -> None:
    out_profile = profile.copy()
    out_profile.update(
        dtype="uint8",
        nodata=0,
        count=1,
        compress="deflate",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **out_profile) as dst:
        dst.write(hillshade_u8, 1)


def _rasterize_kml(kml_path: Path, transform, crs, shape: tuple[int, int]) -> np.ndarray:
    features = parse_kml(kml_path)
    lines = features.descent_lines
    if lines.empty:
        return np.zeros(shape, dtype=np.uint8)
    proj = lines.to_crs(crs)
    geoms = [(g, 1) for g in proj.geometry if not g.is_empty]
    if not geoms:
        return np.zeros(shape, dtype=np.uint8)
    return rasterize(
        geoms, out_shape=shape, transform=transform, fill=0, dtype=np.uint8, all_touched=True
    )


def export_ravine_compare(
    dem_path: str | Path,
    out_dir: str | Path,
    *,
    geomorphon_scales_m: tuple[float, ...] = (30.0, 100.0),
    tpi_scales_m: tuple[float, ...] = (5.0, 10.0, 30.0, 100.0),
    tpi_combine: str = "normalized_sum",
    tpi_smooth_m: float = 1.0,
    tpi_norm_pct: float = 99.0,
    curvature_smooth_m: float = 5.0,
    kml_path: str | Path | None = None,
    kml_opacity: float = 1.0,
    kml_width_px: int = 5,
    display_pct: tuple[float, float] = (2.0, 98.0),
    display_max_px: int = 4000,
    alpha_gamma: float = 0.7,
    keep_geomorphon_work: bool = False,
) -> RavineCompareResult:
    dem_path = Path(dem_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"ravine-compare: {dem_path} → {out_dir}")

    elev, transform, crs, pixel_size = read_dem(str(dem_path))
    with rasterio.open(dem_path) as src:
        profile = src.profile

    log("computing hillshade")
    hillshade = compute_hillshade(elev, pixel_size)

    log(f"computing A (geomorphon, scales={geomorphon_scales_m}m)")
    work_root = out_dir / "_geomorphon_work" if keep_geomorphon_work else Path(
        tempfile.mkdtemp(prefix="ravine_compare_geo_")
    )
    try:
        a_score, a_meta = compute_a_geomorphon(dem_path, geomorphon_scales_m, work_root)
    finally:
        if not keep_geomorphon_work:
            shutil.rmtree(work_root, ignore_errors=True)

    log(
        f"computing B (multi-scale TPI, scales={tpi_scales_m}m, combine={tpi_combine}, "
        f"smooth_m={tpi_smooth_m})"
    )
    b_score, b_meta = compute_b_tpi(
        elev,
        pixel_size,
        tpi_scales_m,
        combine=tpi_combine,
        smooth_m=tpi_smooth_m,
        norm_pct=tpi_norm_pct,
    )

    log(f"computing C (plan curvature, smooth={curvature_smooth_m}m)")
    c_score, c_meta = compute_c_plan_curvature(elev, pixel_size, curvature_smooth_m)

    geo_tif = out_dir / "a_geomorphon.tif"
    tpi_tif = out_dir / "b_tpi.tif"
    cur_tif = out_dir / "c_curvature.tif"
    hs_tif = out_dir / "hillshade.tif"
    _write_score_geotiff(geo_tif, a_score, profile)
    _write_score_geotiff(tpi_tif, b_score, profile)
    _write_score_geotiff(cur_tif, c_score, profile)
    _write_hillshade_geotiff(hs_tif, hillshade, profile)

    kml_mask = None
    if kml_path is not None:
        log(f"rasterizing KML overlay: {kml_path}")
        kml_mask = _rasterize_kml(Path(kml_path), transform, crs, elev.shape)

    a01, a_lo, a_hi = _percentile_stretch(a_score, *display_pct)
    b01, b_lo, b_hi = _percentile_stretch(b_score, *display_pct)
    c01, c_lo, c_hi = _percentile_stretch(c_score, *display_pct)

    hs_panel = Image.fromarray(np.repeat(hillshade[..., None], 3, axis=2), mode="RGB")
    a_panel = _blend_over_hillshade(hillshade, a01, alpha_gamma=alpha_gamma)
    b_panel = _blend_over_hillshade(hillshade, b01, alpha_gamma=alpha_gamma)
    c_panel = _blend_over_hillshade(hillshade, c01, alpha_gamma=alpha_gamma)

    if kml_mask is not None and kml_mask.any():
        for img in (hs_panel, a_panel, b_panel, c_panel):
            _overlay_kml_mask(img, kml_mask, opacity=kml_opacity, width_px=kml_width_px)

    a_label = "A. geomorphon (hollow|valley count)"
    scales_str = ",".join(f"{int(round(s))}" if s == int(s) else f"{s:g}" for s in tpi_scales_m)
    b_label = f"B. multi-scale TPI [{tpi_combine}, {scales_str}m]"
    c_label = "C. plan curvature"
    hs_label = "hillshade" + (" + KML" if kml_mask is not None and kml_mask.any() else "")

    hs_panel.save(out_dir / "panel_hillshade.png")
    a_panel.save(out_dir / "panel_a_geomorphon.png")
    b_panel.save(out_dir / "panel_b_tpi.png")
    c_panel.save(out_dir / "panel_c_curvature.png")

    panels_small = [
        _label_panel(_downsample(hs_panel, display_max_px // 2), hs_label),
        _label_panel(_downsample(a_panel, display_max_px // 2), a_label),
        _label_panel(_downsample(b_panel, display_max_px // 2), b_label),
        _label_panel(_downsample(c_panel, display_max_px // 2), c_label),
    ]
    compare = _grid_2x2(panels_small)
    compare_path = out_dir / "compare.png"
    compare.save(compare_path)

    summary = {
        "dem_path": str(dem_path),
        "out_dir": str(out_dir),
        "pixel_size_m": pixel_size,
        "shape": list(elev.shape),
        "kml_path": str(kml_path) if kml_path else None,
        "display_pct": list(display_pct),
        "alpha_gamma": alpha_gamma,
        "a_geomorphon": {**a_meta, "display_lo": a_lo, "display_hi": a_hi},
        "b_tpi": {**b_meta, "display_lo": b_lo, "display_hi": b_hi},
        "c_curvature": {**c_meta, "display_lo": c_lo, "display_hi": c_hi},
        "outputs": {
            "geomorphon_tif": str(geo_tif),
            "tpi_tif": str(tpi_tif),
            "curvature_tif": str(cur_tif),
            "hillshade_tif": str(hs_tif),
            "compare_png": str(compare_path),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    log(f"wrote {compare_path}")

    return RavineCompareResult(
        dem_path=str(dem_path),
        out_dir=str(out_dir),
        geomorphon_path=str(geo_tif),
        tpi_path=str(tpi_tif),
        curvature_path=str(cur_tif),
        hillshade_path=str(hs_tif),
        compare_png_path=str(compare_path),
    )


def ravine_compare_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ravine-compare",
        description=(
            "Compute three label-free ravine-network signals (geomorphon, "
            "multi-scale TPI, plan curvature) on the same DEM and render a "
            "side-by-side hillshade comparison PNG plus per-signal GeoTIFFs."
        ),
    )
    p.add_argument("dem", type=Path, help="Input DEM (projected, meters).")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument(
        "--geomorphon-scales",
        type=_parse_scales,
        default=(30.0, 100.0),
        help="Comma-separated geomorphon search radii in meters (default: 30,100).",
    )
    p.add_argument(
        "--tpi-scales",
        type=_parse_scales,
        default=(5.0, 10.0, 30.0, 100.0),
        help="Comma-separated TPI radii in meters (default: 5,10,30,100).",
    )
    p.add_argument(
        "--tpi-combine",
        choices=("sum", "normalized_sum", "min"),
        default="normalized_sum",
        help=(
            "How per-scale negative TPI is combined (default: normalized_sum). "
            "'sum' is the legacy raw additive mode (300m-dominated); "
            "'min' takes per-pixel min of normalized scales (concave at every radius)."
        ),
    )
    p.add_argument(
        "--tpi-smooth-m",
        type=float,
        default=1.0,
        help="Gaussian pre-smooth radius (m) for TPI (default: 1). Useful at small radii.",
    )
    p.add_argument(
        "--tpi-norm-pct",
        type=float,
        default=99.0,
        help="Per-scale normalization percentile for combine!=sum (default: 99).",
    )
    p.add_argument(
        "--curvature-smooth-m",
        type=float,
        default=5.0,
        help="Gaussian smoothing radius (m) before plan curvature (default: 5).",
    )
    p.add_argument("--kml", type=Path, default=None, help="Optional KML to overlay.")
    p.add_argument(
        "--kml-opacity",
        type=float,
        default=1.0,
        help="Opacity of the KML overlay [0..1] (default: 1.0).",
    )
    p.add_argument(
        "--kml-width-px",
        type=int,
        default=5,
        help="Width of the overlaid KML line in pixels (default: 5).",
    )
    p.add_argument(
        "--display-pct",
        type=str,
        default="2,98",
        help="Lower,upper percentile stretch for display (default: 2,98).",
    )
    p.add_argument(
        "--display-max-px",
        type=int,
        default=4000,
        help="Max width of the 2x2 compare panel (default: 4000).",
    )
    p.add_argument(
        "--alpha-gamma",
        type=float,
        default=0.7,
        help="Score-to-alpha exponent for overlay (lower = more opaque, default: 0.7).",
    )
    p.add_argument(
        "--keep-geomorphon-work",
        action="store_true",
        help="Keep intermediate per-scale geomorphon outputs.",
    )
    args = p.parse_args(argv)

    lo_str, hi_str = args.display_pct.split(",")
    display_pct = (float(lo_str), float(hi_str))

    export_ravine_compare(
        args.dem,
        args.outdir,
        geomorphon_scales_m=tuple(args.geomorphon_scales),
        tpi_scales_m=tuple(args.tpi_scales),
        tpi_combine=args.tpi_combine,
        tpi_smooth_m=args.tpi_smooth_m,
        tpi_norm_pct=args.tpi_norm_pct,
        curvature_smooth_m=args.curvature_smooth_m,
        kml_path=args.kml,
        kml_opacity=args.kml_opacity,
        kml_width_px=args.kml_width_px,
        display_pct=display_pct,
        display_max_px=args.display_max_px,
        alpha_gamma=args.alpha_gamma,
        keep_geomorphon_work=args.keep_geomorphon_work,
    )

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.features import rasterize
from rasterio.windows import Window
from scipy import ndimage as ndi

from .io import log
from .validate import parse_kml


@dataclass(frozen=True)
class DropPreviewResult:
    distance_m: float
    raster_path: str
    preview_path: str
    log_preview_path: str
    quantiles_m: dict[str, float]
    route_quantiles_m: dict[str, float]


def _parse_distances(value: str) -> tuple[float, ...]:
    distances = tuple(float(part) for part in value.replace(" ", "").split(",") if part)
    if not distances:
        raise argparse.ArgumentTypeError("expected at least one distance in meters")
    if any(distance <= 0 for distance in distances):
        raise argparse.ArgumentTypeError("distances must be positive")
    return distances


def _safe_scale_label(distance_m: float) -> str:
    return f"{distance_m:g}m"


def _safe_stem(path: str | Path) -> str:
    stem = Path(path).stem
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem).strip("_") or "dem"


def _circular_footprint(radius_px: int) -> np.ndarray:
    radius_px = max(1, int(radius_px))
    yy, xx = np.ogrid[-radius_px : radius_px + 1, -radius_px : radius_px + 1]
    return (xx * xx + yy * yy) <= radius_px * radius_px


def drop_to_lower_ground(
    elev: np.ma.MaskedArray | np.ndarray,
    pixel_size: float,
    distance_m: float,
) -> np.ndarray:
    """Return elevation minus the minimum elevation within a circular distance window."""
    mask = np.ma.getmaskarray(elev)
    data = np.ma.asarray(elev).filled(np.inf).astype(np.float32)
    radius_px = max(1, round(distance_m / max(pixel_size, 1e-6)))
    local_min = ndi.minimum_filter(
        data,
        footprint=_circular_footprint(radius_px),
        mode="constant",
        cval=np.inf,
    )
    with np.errstate(invalid="ignore"):
        drop = data - local_min
    drop[mask | ~np.isfinite(drop)] = np.nan
    return np.maximum(drop, 0.0).astype(np.float32)


def _write_drop_raster(path: Path, drop: np.ndarray, profile: dict) -> None:
    nodata = -9999.0
    out = np.where(np.isfinite(drop), drop, nodata).astype(np.float32)
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


_RAMP = np.array(
    [
        (0, 0, 4),
        (32, 12, 72),
        (97, 30, 104),
        (160, 42, 100),
        (221, 73, 79),
        (252, 140, 89),
        (254, 205, 141),
        (252, 253, 191),
    ],
    dtype=np.float32,
)


def _colorize(values: np.ndarray, vmax: float) -> Image.Image:
    arr = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(arr)
    denom = max(float(vmax), 1e-6)
    scaled = np.clip(np.where(finite, arr / denom, 0.0), 0.0, 1.0)
    pos = scaled * (len(_RAMP) - 1)
    lo = np.floor(pos).astype(np.int16)
    hi = np.clip(lo + 1, 0, len(_RAMP) - 1)
    frac = (pos - lo)[..., None]
    rgb = _RAMP[lo] * (1.0 - frac) + _RAMP[hi] * frac
    rgb[~finite] = (0, 0, 0)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")


def _overlay_route(image: Image.Image, route_mask: np.ndarray) -> None:
    rgb = np.asarray(image).copy()
    mask = route_mask.astype(bool)
    if mask.shape == rgb.shape[:2]:
        rgb[mask] = (0, 255, 255)
        # Light dilation makes the line visible after downsampling.
        thick = ndi.binary_dilation(mask, iterations=1)
        rgb[thick & ~mask] = (0, 180, 190)
    image.paste(Image.fromarray(rgb, mode="RGB"))


def _font(size: int = 18) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _draw_title(panel: Image.Image, title: str) -> Image.Image:
    title_h = 34
    out = Image.new("RGB", (panel.width, panel.height + title_h), "white")
    out.paste(panel, (0, title_h))
    draw = ImageDraw.Draw(out)
    draw.text((8, 6), title, fill=(0, 0, 0), font=_font(18))
    return out


def _write_panel(
    path: Path,
    values: np.ndarray,
    route_mask: np.ndarray,
    *,
    title: str,
    vmax: float,
) -> None:
    image = _colorize(values, vmax)
    _overlay_route(image, route_mask)
    image = _draw_title(image, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _write_comparison(
    path: Path,
    panels: list[tuple[str, np.ndarray]],
    route_mask: np.ndarray,
    *,
    vmax: float,
    title: str,
) -> None:
    rendered = []
    for label, values in panels:
        image = _colorize(values, vmax)
        _overlay_route(image, route_mask)
        rendered.append(_draw_title(image, label))

    gap = 18
    top_h = 42
    width = sum(panel.width for panel in rendered) + gap * (len(rendered) - 1)
    height = top_h + max(panel.height for panel in rendered)
    out = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(out)
    draw.text((8, 8), title, fill=(0, 0, 0), font=_font(22))
    x = 0
    for panel in rendered:
        out.paste(panel, (x, top_h))
        x += panel.width + gap
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path)


def _stats(drop: np.ndarray, route_mask: np.ndarray) -> tuple[dict[str, float], dict[str, float]]:
    vals = drop[np.isfinite(drop)]
    quantiles = {
        str(q): float(np.percentile(vals, q))
        for q in (0, 50, 75, 90, 95, 99, 99.5, 99.9, 100)
    }
    route_vals = drop[route_mask & np.isfinite(drop)]
    route_quantiles = (
        {str(q): float(np.percentile(route_vals, q)) for q in (0, 50, 75, 90, 95, 99, 100)}
        if route_vals.size
        else {}
    )
    return quantiles, route_quantiles


def _crop_window_for_route(src, kml_path: Path, crop_buffer_m: float) -> tuple[Window, np.ndarray]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError(f"KML has no descent lines: {kml_path}")
    route = features.descent_lines.to_crs(src.crs)
    line = route.geometry.union_all()
    minx, miny, maxx, maxy = line.buffer(crop_buffer_m).bounds
    window = src.window(minx, miny, maxx, maxy).round_offsets().round_lengths()
    row0 = max(0, int(window.row_off))
    col0 = max(0, int(window.col_off))
    row1 = min(src.height, row0 + int(window.height))
    col1 = min(src.width, col0 + int(window.width))
    clipped = Window(col0, row0, max(1, col1 - col0), max(1, row1 - row0))
    route_mask = rasterize(
        ((geom, 1) for geom in route.geometry),
        out_shape=src.shape,
        transform=src.transform,
        all_touched=True,
        fill=0,
        dtype="uint8",
    ).astype(bool)
    return clipped, route_mask


def export_drop_previews(
    dem_path: str | Path,
    kml_path: str | Path,
    out_dir: str | Path,
    *,
    distances_m: tuple[float, ...] = (5.0, 10.0, 20.0),
    crop_buffer_m: float = 900.0,
    preview_percentile: float = 99.5,
) -> dict[str, object]:
    dem_path = Path(dem_path)
    kml_path = Path(kml_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = _safe_stem(kml_path)
    results: list[DropPreviewResult] = []
    comparison_panels: list[tuple[str, np.ndarray]] = []
    log_comparison_panels: list[tuple[str, np.ndarray]] = []

    with rasterio.open(dem_path) as src:
        elev = src.read(1, masked=True)
        profile = src.profile.copy()
        pixel_size = float(abs(src.transform.a))
        window, route_mask = _crop_window_for_route(src, kml_path, crop_buffer_m)
        rs = slice(int(window.row_off), int(window.row_off + window.height))
        cs = slice(int(window.col_off), int(window.col_off + window.width))
        route_crop = route_mask[rs, cs]

        for distance_m in distances_m:
            scale = _safe_scale_label(distance_m)
            log(f"building drop preview distance={scale}")
            drop = drop_to_lower_ground(elev, pixel_size, distance_m)
            raster_path = out_dir / f"{stem}_drop_to_lower_ground_{scale}.tif"
            preview_path = out_dir / f"{stem}_drop_to_lower_ground_{scale}_preview.png"
            log_preview_path = out_dir / f"{stem}_drop_to_lower_ground_{scale}_log_preview.png"
            _write_drop_raster(raster_path, drop, profile)

            crop = drop[rs, cs]
            crop_vmax = float(np.nanpercentile(crop, preview_percentile))
            _write_panel(
                preview_path,
                crop,
                route_crop,
                title=f"{scale} drop_to_lower_ground, clipped p{preview_percentile:g}",
                vmax=crop_vmax,
            )
            log_crop = np.log1p(crop)
            log_vmax = float(np.nanpercentile(log_crop, preview_percentile))
            _write_panel(
                log_preview_path,
                log_crop,
                route_crop,
                title=f"log1p {scale} drop_to_lower_ground, clipped p{preview_percentile:g}",
                vmax=log_vmax,
            )
            quantiles, route_quantiles = _stats(drop, route_mask)
            results.append(
                DropPreviewResult(
                    distance_m=distance_m,
                    raster_path=str(raster_path),
                    preview_path=str(preview_path),
                    log_preview_path=str(log_preview_path),
                    quantiles_m=quantiles,
                    route_quantiles_m=route_quantiles,
                )
            )
            comparison_panels.append((scale, crop))
            log_comparison_panels.append((f"log1p {scale}", log_crop))

    comparison_path = out_dir / f"{stem}_drop_to_lower_ground_comparison.png"
    log_comparison_path = out_dir / f"{stem}_drop_to_lower_ground_log_comparison.png"
    shared_vmax = max(float(np.nanpercentile(values, preview_percentile)) for _, values in comparison_panels)
    shared_log_vmax = max(
        float(np.nanpercentile(values, preview_percentile)) for _, values in log_comparison_panels
    )
    _write_comparison(
        comparison_path,
        comparison_panels,
        route_crop,
        vmax=shared_vmax,
        title=f"{stem}: drop_to_lower_ground comparison",
    )
    _write_comparison(
        log_comparison_path,
        log_comparison_panels,
        route_crop,
        vmax=shared_log_vmax,
        title=f"{stem}: log1p drop_to_lower_ground comparison",
    )

    summary: dict[str, object] = {
        "source_dem": str(dem_path),
        "kml_path": str(kml_path),
        "distances_m": list(distances_m),
        "crop_buffer_m": crop_buffer_m,
        "preview_percentile": preview_percentile,
        "comparison_png": str(comparison_path),
        "log_comparison_png": str(log_comparison_path),
        "results": [result.__dict__ for result in results],
    }
    summary_path = out_dir / f"{stem}_drop_to_lower_ground_stats.json"
    summary["stats_json"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def drop_previews_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="canyongis-drop-previews",
        description="Build DEM drop-to-lower-ground rasters and route-overlay preview PNGs.",
    )
    parser.add_argument("dem", type=Path, help="Input projected DEM GeoTIFF.")
    parser.add_argument("kml", type=Path, help="KML with a descent line to overlay/crop around.")
    parser.add_argument("outdir", type=Path, help="Output directory.")
    parser.add_argument(
        "--distances-m",
        type=_parse_distances,
        default=(5.0, 10.0, 20.0),
        help="Comma-separated search distances in meters (default: 5,10,20).",
    )
    parser.add_argument(
        "--crop-buffer-m",
        type=float,
        default=900.0,
        help="Preview crop buffer around descent lines in meters (default: 900).",
    )
    parser.add_argument(
        "--preview-percentile",
        type=float,
        default=99.5,
        help="Robust upper percentile for preview color scaling (default: 99.5).",
    )
    args = parser.parse_args(argv)
    summary = export_drop_previews(
        args.dem,
        args.kml,
        args.outdir,
        distances_m=args.distances_m,
        crop_buffer_m=args.crop_buffer_m,
        preview_percentile=args.preview_percentile,
    )
    log(f"wrote drop previews to {args.outdir}")
    log(f"comparison: {summary['comparison_png']}")
    log(f"log comparison: {summary['log_comparison_png']}")

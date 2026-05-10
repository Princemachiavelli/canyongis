from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from .hydrology import compute_flow_accum
from .io import log, read_dem, write_raster
from .ravine_compare import _blend_over_hillshade, _percentile_stretch, compute_hillshade


@dataclass(frozen=True)
class DrainagePreviewResult:
    dem_path: str
    out_dir: str
    flow_accum_path: str
    flow_preview_path: str
    stream_preview_path: str
    thresholds: tuple[int, ...]
    stream_pixels: dict[str, int]


def _parse_thresholds(value: str) -> tuple[int, ...]:
    thresholds = tuple(int(part) for part in value.replace(" ", "").split(",") if part)
    if not thresholds:
        raise argparse.ArgumentTypeError("expected at least one flow accumulation threshold")
    if any(threshold <= 0 for threshold in thresholds):
        raise argparse.ArgumentTypeError("thresholds must be positive")
    return tuple(sorted(set(thresholds)))


def _write_mask(path: Path, mask: np.ndarray, transform, crs) -> None:
    write_raster(str(path), mask.astype(np.uint8), transform, crs, dtype="uint8")


def _stream_network_preview(
    hillshade: np.ndarray,
    flow_accum: np.ndarray,
    thresholds: tuple[int, ...],
    *,
    line_width_px: int = 1,
) -> Image.Image:
    rgb = np.repeat(hillshade[..., None], 3, axis=2).astype(np.uint8)
    colors = np.asarray(
        [
            (0, 255, 255),
            (0, 140, 255),
            (255, 220, 0),
            (255, 80, 0),
            (255, 0, 255),
        ],
        dtype=np.uint8,
    )
    try:
        from scipy import ndimage as ndi
    except Exception:
        ndi = None

    for idx, threshold in enumerate(thresholds):
        mask = np.isfinite(flow_accum) & (flow_accum >= threshold)
        if line_width_px > 1 and ndi is not None:
            mask = ndi.binary_dilation(mask, iterations=max(0, line_width_px // 2))
        rgb[mask] = colors[min(idx, len(colors) - 1)]
    return Image.fromarray(rgb, mode="RGB")


def export_drainage_previews(
    dem_path: str | Path,
    out_dir: str | Path,
    *,
    thresholds: tuple[int, ...] = (200, 500, 1000, 5000),
    fill_depressions: bool = True,
    preview_hi_pct: float = 99.5,
    line_width_px: int = 1,
) -> DrainagePreviewResult:
    dem_path = Path(dem_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"drainage-preview: dem={dem_path} → {out_dir}")
    elev, transform, crs, pixel_size = read_dem(str(dem_path))
    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()

    log("computing D8 flow accumulation")
    flow_accum = compute_flow_accum(elev, transform, fill_depressions=fill_depressions)
    flow_accum[~np.isfinite(elev)] = np.nan

    flow_path = out_dir / "flow_accum.tif"
    nodata = -9999.0
    flow_profile = profile.copy()
    flow_profile.update(
        dtype="float32",
        nodata=nodata,
        count=1,
        compress="deflate",
        predictor=3,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with rasterio.open(flow_path, "w", **flow_profile) as dst:
        dst.write(np.where(np.isfinite(flow_accum), flow_accum, nodata).astype(np.float32), 1)

    hillshade = compute_hillshade(elev, pixel_size)
    flow_log = np.log1p(np.clip(flow_accum, 0.0, None)).astype(np.float32)
    flow01, lo, hi = _percentile_stretch(flow_log, 1.0, preview_hi_pct)
    flow_preview = _blend_over_hillshade(hillshade, flow01, alpha_gamma=0.65)
    flow_preview_path = out_dir / "flow_accum_log_preview.png"
    flow_preview.save(flow_preview_path)

    stream_pixels: dict[str, int] = {}
    for threshold in thresholds:
        mask = np.isfinite(flow_accum) & (flow_accum >= threshold)
        stream_pixels[str(threshold)] = int(mask.sum())
        _write_mask(out_dir / f"stream_mask_t{threshold}.tif", mask, transform, crs)

    stream_preview = _stream_network_preview(
        hillshade,
        flow_accum,
        thresholds,
        line_width_px=line_width_px,
    )
    stream_preview_path = out_dir / "stream_network_preview.png"
    stream_preview.save(stream_preview_path)

    summary = {
        "dem_path": str(dem_path),
        "out_dir": str(out_dir),
        "pixel_size_m": pixel_size,
        "fill_depressions": fill_depressions,
        "thresholds": thresholds,
        "stream_pixels": stream_pixels,
        "flow_log_display_lo": lo,
        "flow_log_display_hi": hi,
        "outputs": {
            "flow_accum_tif": str(flow_path),
            "flow_accum_log_preview_png": str(flow_preview_path),
            "stream_network_preview_png": str(stream_preview_path),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    return DrainagePreviewResult(
        dem_path=str(dem_path),
        out_dir=str(out_dir),
        flow_accum_path=str(flow_path),
        flow_preview_path=str(flow_preview_path),
        stream_preview_path=str(stream_preview_path),
        thresholds=thresholds,
        stream_pixels=stream_pixels,
    )


def drainage_previews_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-drainage-previews",
        description="Compute D8 flow accumulation and render colored drainage-network previews.",
    )
    p.add_argument("dem", type=Path, help="Input DEM GeoTIFF.")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument(
        "--thresholds",
        type=_parse_thresholds,
        default=(200, 500, 1000, 5000),
        help="Comma-separated flow accumulation thresholds for stream masks (default: 200,500,1000,5000).",
    )
    p.add_argument(
        "--no-flow-fill-depressions",
        action="store_true",
        help="Skip pysheds depression filling before flow accumulation.",
    )
    p.add_argument(
        "--preview-hi-pct",
        type=float,
        default=99.5,
        help="Upper percentile for log-flow color scaling (default: 99.5).",
    )
    p.add_argument(
        "--line-width-px",
        type=int,
        default=1,
        help="Preview stream line dilation width in pixels (default: 1).",
    )
    args = p.parse_args(argv)

    result = export_drainage_previews(
        args.dem,
        args.outdir,
        thresholds=args.thresholds,
        fill_depressions=not args.no_flow_fill_depressions,
        preview_hi_pct=args.preview_hi_pct,
        line_width_px=args.line_width_px,
    )
    print(json.dumps(asdict(result), indent=2))

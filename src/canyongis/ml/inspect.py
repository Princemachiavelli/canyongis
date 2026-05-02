from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from ..io import log
from .config import MLConfig
from .dataset import load_manifest
from .labels import build_labels_from_kml_paths


def _band_names(count: int) -> list[str]:
    names = MLConfig().channel_names()
    if count == len(names):
        return names
    return [f"band_{i + 1}" for i in range(count)]


def _scale_to_u8(
    arr: np.ndarray,
    *,
    lower_pct: float,
    upper_pct: float,
) -> tuple[np.ndarray, dict[str, float]]:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8), {"lo": 0.0, "hi": 0.0}

    lo = float(np.percentile(finite, lower_pct))
    hi = float(np.percentile(finite, upper_pct))
    if hi <= lo:
        hi = lo + 1e-6

    scaled = np.clip((np.nan_to_num(arr, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
    return (scaled * 255.0).astype(np.uint8), {"lo": lo, "hi": hi}


def export_feature_pngs(
    features_tif: str | Path,
    out_dir: str | Path,
    *,
    lower_pct: float = 2.0,
    upper_pct: float = 98.0,
) -> dict[str, object]:
    """Export each band of a feature GeoTIFF to an 8-bit grayscale PNG."""
    features_tif = Path(features_tif)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(features_tif) as src:
        count = src.count
        names = _band_names(count)
        manifest: list[dict[str, object]] = []

        for idx in range(1, count + 1):
            name = names[idx - 1]
            arr = src.read(idx).astype(np.float32)
            png, stats = _scale_to_u8(arr, lower_pct=lower_pct, upper_pct=upper_pct)
            out_path = out_dir / f"{idx:02d}_{name}.png"
            Image.fromarray(png, mode="L").save(out_path)
            manifest.append(
                {
                    "band": idx,
                    "name": name,
                    "png_path": str(out_path),
                    "lower_percentile": lower_pct,
                    "upper_percentile": upper_pct,
                    "display_min": stats["lo"],
                    "display_max": stats["hi"],
                }
            )
            log(f"wrote {out_path.name}")

    summary = {
        "source_tif": str(features_tif),
        "out_dir": str(out_dir),
        "bands": manifest,
    }
    (out_dir / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def export_feature_pngs_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ml-export-features",
        description="Export a multiband feature GeoTIFF to viewable grayscale PNGs.",
    )
    p.add_argument("features_tif", type=Path, help="Input multiband feature GeoTIFF.")
    p.add_argument("outdir", type=Path, help="Output directory for PNGs.")
    p.add_argument(
        "--lower-pct",
        type=float,
        default=2.0,
        help="Lower display percentile for contrast stretch (default: 2).",
    )
    p.add_argument(
        "--upper-pct",
        type=float,
        default=98.0,
        help="Upper display percentile for contrast stretch (default: 98).",
    )
    args = p.parse_args(argv)
    export_feature_pngs(
        args.features_tif,
        args.outdir,
        lower_pct=args.lower_pct,
        upper_pct=args.upper_pct,
    )


def export_canyon_labels(
    manifest_path: str | Path,
    out_dir: str | Path,
    *,
    cfg: MLConfig | None = None,
    label_sigma_m: float | None = None,
    rappel_sigma_m: float | None = None,
) -> dict[str, object]:
    """Render full-DEM canyon (and rappel) label rasters as GeoTIFFs.

    For each manifest entry, computes the same Gaussian soft labels used during
    training, at the source DEM's full resolution and grid, and writes them as
    geo-referenced GeoTIFFs that can be overlaid on the DEM in QGIS to verify
    label completeness.
    """
    manifest_path = Path(manifest_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg if cfg is not None else MLConfig()

    entries = load_manifest(manifest_path)
    summary: list[dict[str, object]] = []
    for entry in entries:
        with rasterio.open(entry.dem_path) as src:
            dem_shape = (src.height, src.width)
            dem_transform = src.transform
            dem_crs = src.crs
            pixel_size = float(abs(dem_transform.a))
            profile = src.profile
        canyon_y, rappel_y = build_labels_from_kml_paths(
            entry.kml_paths,
            dem_transform,
            dem_crs,
            dem_shape,
            pixel_size,
            cfg,
            label_sigma_m=label_sigma_m,
            rappel_sigma_m=rappel_sigma_m,
        )
        canyon_path = out_dir / f"{entry.dem_id}_canyon_label.tif"
        rappel_path = out_dir / f"{entry.dem_id}_rappel_label.tif"
        out_profile = {
            **profile,
            "count": 1,
            "dtype": "float32",
            "compress": "deflate",
            "predictor": 3,
            "nodata": None,
        }
        with rasterio.open(canyon_path, "w", **out_profile) as dst:
            dst.write(canyon_y.astype(np.float32), 1)
        with rasterio.open(rappel_path, "w", **out_profile) as dst:
            dst.write(rappel_y.astype(np.float32), 1)
        coverage = float((canyon_y > 0).mean())
        peak_pixels = int((canyon_y >= 0.99).sum())
        summary.append(
            {
                "dem_id": entry.dem_id,
                "dem_path": entry.dem_path,
                "kml_paths": list(entry.kml_paths),
                "canyon_label_tif": str(canyon_path),
                "rappel_label_tif": str(rappel_path),
                "canyon_coverage_fraction": coverage,
                "canyon_centerline_pixels": peak_pixels,
                "label_sigma_m": label_sigma_m if label_sigma_m is not None else cfg.label_sigma_m,
            }
        )
        log(
            f"wrote {canyon_path.name} "
            f"(coverage={coverage * 100:.3f}% centerline_px={peak_pixels})"
        )

    out_summary = {"manifest": str(manifest_path), "out_dir": str(out_dir), "entries": summary}
    (out_dir / "manifest.json").write_text(json.dumps(out_summary, indent=2) + "\n")
    return out_summary


def export_canyon_labels_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ml-export-labels",
        description=(
            "Render the canyon and rappel soft-label rasters used during training as "
            "geo-referenced GeoTIFFs, one pair per manifest entry, for QGIS inspection."
        ),
    )
    p.add_argument("manifest", type=Path, help="Training manifest JSON path.")
    p.add_argument("outdir", type=Path, help="Output directory for label GeoTIFFs.")
    p.add_argument(
        "--label-sigma-m",
        type=float,
        default=None,
        help="Override canyon Gaussian sigma (m). Defaults to MLConfig.label_sigma_m.",
    )
    p.add_argument(
        "--rappel-sigma-m",
        type=float,
        default=None,
        help="Override rappel Gaussian sigma (m). Defaults to MLConfig.rappel_sigma_m.",
    )
    p.add_argument(
        "--label-floor",
        type=float,
        default=None,
        help="Override label floor (clamp Gaussian below this value to 0).",
    )
    args = p.parse_args(argv)
    cfg = MLConfig()
    if args.label_floor is not None:
        cfg.label_floor = args.label_floor
    export_canyon_labels(
        args.manifest,
        args.outdir,
        cfg=cfg,
        label_sigma_m=args.label_sigma_m,
        rappel_sigma_m=args.rappel_sigma_m,
    )

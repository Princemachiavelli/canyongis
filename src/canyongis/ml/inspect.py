from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from ..io import log
from .config import MLConfig


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

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
from affine import Affine
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from pyproj import Transformer
from rasterio.crs import CRS

from .io import log

Corner = Literal["nw", "ne", "sw", "se"]

FORM_CLASSES: dict[int, str] = {
    1: "flat",
    2: "peak",
    3: "ridge",
    4: "shoulder",
    5: "spur",
    6: "slope",
    7: "hollow",
    8: "footslope",
    9: "valley",
    10: "pit",
}

FORM_COLORS: dict[int, tuple[int, int, int]] = {
    1: (202, 206, 210),
    2: (148, 58, 150),
    3: (219, 75, 65),
    4: (230, 139, 54),
    5: (230, 190, 72),
    6: (160, 146, 102),
    7: (88, 183, 180),
    8: (104, 174, 92),
    9: (69, 118, 198),
    10: (95, 74, 145),
}


@dataclass(frozen=True)
class PreviewResult:
    source_tif: str
    png_path: str
    corners: dict[str, dict[str, float]]
    center: dict[str, float]


def _parse_corners(value: str) -> tuple[Corner, ...]:
    if value.strip().lower() == "all":
        return ("nw", "ne", "sw", "se")
    corners = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    valid = {"nw", "ne", "sw", "se"}
    bad = [corner for corner in corners if corner not in valid]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown corner(s): {', '.join(bad)}")
    if not corners:
        raise argparse.ArgumentTypeError("at least one corner is required")
    return corners  # type: ignore[return-value]


def _corner_xy(transform: Affine, width: int, height: int) -> dict[Corner, tuple[float, float]]:
    return {
        "nw": transform * (0, 0),
        "ne": transform * (width, 0),
        "sw": transform * (0, height),
        "se": transform * (width, height),
    }


def _latlon_points(
    transform: Affine,
    width: int,
    height: int,
    crs: CRS,
) -> tuple[dict[Corner, dict[str, float]], dict[str, float]]:
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    corners: dict[Corner, dict[str, float]] = {}
    for corner, (x, y) in _corner_xy(transform, width, height).items():
        lon, lat = transformer.transform(x, y)
        corners[corner] = {"lat": float(lat), "lon": float(lon)}

    center_x, center_y = transform * (width / 2.0, height / 2.0)
    center_lon, center_lat = transformer.transform(center_x, center_y)
    return corners, {"lat": float(center_lat), "lon": float(center_lon)}


def _forms_to_rgb(forms: np.ndarray, mask: np.ndarray) -> np.ndarray:
    rgb = np.zeros((*forms.shape, 3), dtype=np.uint8)
    rgb[:] = (30, 32, 34)
    for value, color in FORM_COLORS.items():
        rgb[(forms == value) & ~mask] = color
    return rgb


def _load_font(width: int, height: int) -> ImageFont.ImageFont:
    size = max(14, min(44, min(width, height) // 80))
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _draw_label(
    draw: ImageDraw.ImageDraw,
    image_size: tuple[int, int],
    corner: Corner,
    text: str,
    font: ImageFont.ImageFont,
) -> None:
    width, height = image_size
    pad = max(8, min(width, height) // 160)
    box_pad = max(4, pad // 2)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    if corner == "nw":
        x, y = pad, pad
    elif corner == "ne":
        x, y = width - pad - text_w, pad
    elif corner == "sw":
        x, y = pad, height - pad - text_h
    else:
        x, y = width - pad - text_w, height - pad - text_h

    draw.rectangle(
        (x - box_pad, y - box_pad, x + text_w + box_pad, y + text_h + box_pad),
        fill=(0, 0, 0),
    )
    draw.text((x, y), text, fill=(255, 255, 255), font=font)


def _png_metadata(result: PreviewResult) -> PngImagePlugin.PngInfo:
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("source_tif", result.source_tif)
    pnginfo.add_text("center_lat", f"{result.center['lat']:.8f}")
    pnginfo.add_text("center_lon", f"{result.center['lon']:.8f}")
    for corner, latlon in result.corners.items():
        pnginfo.add_text(f"{corner}_lat", f"{latlon['lat']:.8f}")
        pnginfo.add_text(f"{corner}_lon", f"{latlon['lon']:.8f}")
    return pnginfo


def export_form_preview_png(
    forms_tif: str | Path,
    out_png: str | Path | None = None,
    *,
    corners: tuple[Corner, ...] = ("nw", "se"),
) -> PreviewResult:
    forms_tif = Path(forms_tif)
    if out_png is None:
        out_png = forms_tif.parent / "pngs" / "geomorphon_forms_classes_with_corners.png"
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(forms_tif) as src:
        if src.crs is None:
            raise ValueError(f"geomorphon forms raster must have a CRS: {forms_tif}")
        arr = src.read(1, masked=True)
        data = np.asarray(arr.filled(0), dtype=np.uint8)
        mask = np.ma.getmaskarray(arr)
        all_corners, center = _latlon_points(src.transform, src.width, src.height, src.crs)

    rgb = _forms_to_rgb(data, mask)
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    font = _load_font(image.width, image.height)
    selected_corners = {corner: all_corners[corner] for corner in corners}
    for corner, latlon in selected_corners.items():
        label = f"{corner.upper()} {latlon['lat']:.6f}, {latlon['lon']:.6f}"
        _draw_label(draw, image.size, corner, label, font)

    result = PreviewResult(
        source_tif=str(forms_tif),
        png_path=str(out_png),
        corners={corner: selected_corners[corner] for corner in corners},
        center=center,
    )
    image.save(out_png, pnginfo=_png_metadata(result))
    return result


def find_form_rasters(paths: list[Path], pattern: str = "**/geomorphon_forms.tif") -> list[Path]:
    rasters: list[Path] = []
    for path in paths:
        if path.is_file():
            rasters.append(path)
        elif path.is_dir():
            rasters.extend(path.glob(pattern))
        else:
            raise FileNotFoundError(path)
    return sorted(set(rasters))


def export_form_preview_pngs(
    paths: list[str | Path],
    *,
    pattern: str = "**/geomorphon_forms.tif",
    corners: tuple[Corner, ...] = ("nw", "se"),
) -> list[PreviewResult]:
    rasters = find_form_rasters([Path(path) for path in paths], pattern=pattern)
    results = [export_form_preview_png(path, corners=corners) for path in rasters]
    for result in results:
        log(f"wrote {result.png_path}")
    return results


def geomorphon_previews_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-geomorphon-previews",
        description="Render colored geomorphon forms PNG previews with corner lat/lon labels.",
    )
    p.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Input geomorphon_forms.tif file(s) or directories to search recursively.",
    )
    p.add_argument(
        "--pattern",
        default="**/geomorphon_forms.tif",
        help="Glob used for directory inputs (default: **/geomorphon_forms.tif).",
    )
    p.add_argument(
        "--corners",
        type=_parse_corners,
        default=("nw", "se"),
        help="Comma-separated corners to label, or 'all' (default: nw,se).",
    )
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional JSON manifest path for generated previews.",
    )
    args = p.parse_args(argv)

    results = export_form_preview_pngs(
        args.paths,
        pattern=args.pattern,
        corners=args.corners,
    )
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps([result.__dict__ for result in results], indent=2) + "\n"
        )
    log(f"generated {len(results)} geomorphon form preview PNG(s)")

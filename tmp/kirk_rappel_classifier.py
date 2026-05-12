"""Apply ternary-rule rappel detector across the Kirk DEM and render a legended PNG."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.features import rasterize
from scipy import ndimage as ndi

from canyongis.validate import parse_kml

KML = Path("data/ropewiki/kml/Kirk_Creek_Canyon.kml")
DEM = Path("data/ropewiki/dem/mosaic/Kirk_Creek_Canyon_buffer1200m.tif")
GEO = Path("tmp/kirk-geomorph-3m8m")
HILLSHADE = Path("tmp/kirk-previews/Kirk_Creek_Canyon_hillshade.tif")
BANDPASS = Path("tmp/kirk-previews/Kirk_Creek_Canyon_drop_bandpass_2m-5m.tif")
OUT_PNG = Path("tmp/kirk-previews/Kirk_Creek_Canyon_ternary_rappel_classification.png")

# (form_code, ternary_code) → (label, RGB)
RULES: dict[tuple[int, int], tuple[str, tuple[int, int, int]]] = {
    (6, 80): ("slope · 00002222 (rim/wall split)", (255, 64, 64)),  # red
    (5, 26): ("spur · 00000222 (asymmetric rim)", (255, 165, 0)),  # orange
    (7, 242): ("hollow · 00222222 (encased lip)", (255, 230, 0)),  # yellow
}
BANDPASS_THRESHOLD_M = 1.5


def main() -> None:
    with rasterio.open(DEM) as dem:
        crs = dem.crs
    with rasterio.open(HILLSHADE) as src:
        hill = src.read(1)
        tr = src.transform
        h, w = hill.shape
    forms = rasterio.open(GEO / "geomorphon_forms.tif").read(1)
    ternary = rasterio.open(GEO / "geomorphon_ternary.tif").read(1)
    bandpass = rasterio.open(BANDPASS).read(1)

    # Hillshade base, lightened so colored overlay reads.
    base = np.clip(hill.astype(np.float32) * 0.7 + 70, 0, 255).astype(np.uint8)
    rgb = np.stack([base, base, base], axis=-1)

    # Apply rules.
    drop_ok = np.where(np.isfinite(bandpass), bandpass >= BANDPASS_THRESHOLD_M, False)
    matched_total = np.zeros_like(forms, dtype=bool)
    counts: dict[tuple[int, int], int] = {}
    for (fc, tc), (_label, color) in RULES.items():
        mask = (forms == fc) & (ternary == tc) & drop_ok
        counts[(fc, tc)] = int(mask.sum())
        # Dilate so the overlay stays visible at ~half resolution.
        thick = ndi.binary_dilation(mask, iterations=1)
        rgb[thick] = color
        matched_total |= mask

    # Overlay descent line as a hollow cyan ring.
    feats = parse_kml(KML)
    descent = feats.descent_lines.to_crs(crs)
    line_mask = rasterize(
        ((g, 1) for g in descent.geometry),
        out_shape=(h, w),
        transform=tr,
        fill=0,
        dtype=np.uint8,
    ).astype(bool)
    outer = ndi.binary_dilation(line_mask, iterations=4)
    inner = ndi.binary_dilation(line_mask, iterations=2)
    rgb[outer & ~inner] = (0, 255, 255)

    # Render base image.
    img = Image.fromarray(rgb, "RGB")

    # Downsize for a manageable preview.
    max_w = 1400
    if img.width > max_w:
        scale = max_w / img.width
        img = img.resize((max_w, int(img.height * scale)), Image.BILINEAR)

    # Legend strip on the right.
    legend_w = 520
    title_h = 40
    final = Image.new(
        "RGB", (img.width + legend_w, max(img.height, 60 * (len(RULES) + 3)) + title_h), "white"
    )
    final.paste(img, (0, title_h))
    draw = ImageDraw.Draw(final)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
        font_sm = ImageFont.truetype("DejaVuSans.ttf", 14)
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
        font_sm = font
        font_title = font

    draw.text(
        (10, 8),
        f"Kirk Creek — ternary rappel classification (bandpass 2m-5m ≥ {BANDPASS_THRESHOLD_M:.1f}m)",
        fill=(0, 0, 0),
        font=font_title,
    )

    lx = img.width + 16
    ly = title_h + 16
    draw.text((lx, ly), "Form · ternary code (legend)", fill=(0, 0, 0), font=font_title)
    ly += 36
    for (fc, tc), (label, color) in RULES.items():
        draw.rectangle([lx, ly, lx + 28, ly + 22], fill=color, outline=(0, 0, 0))
        draw.text((lx + 38, ly + 1), f"form={fc} · tern={tc}", fill=(0, 0, 0), font=font)
        draw.text(
            (lx + 38, ly + 22),
            f"  {label}  · n={counts[(fc, tc)]}",
            fill=(60, 60, 60),
            font=font_sm,
        )
        ly += 56

    draw.rectangle([lx, ly, lx + 28, ly + 22], fill=(0, 255, 255), outline=(0, 0, 0))
    draw.text((lx + 38, ly + 1), "descent line (KML)", fill=(0, 0, 0), font=font)
    ly += 36
    draw.text(
        (lx, ly + 8),
        f"Total matched pixels: {int(matched_total.sum()):,}",
        fill=(0, 0, 0),
        font=font,
    )

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUT_PNG)
    print(f"wrote {OUT_PNG}")
    print("per-rule counts:")
    for k, n in counts.items():
        print(f"  form={k[0]} ternary={k[1]}: {n:,}")


if __name__ == "__main__":
    main()

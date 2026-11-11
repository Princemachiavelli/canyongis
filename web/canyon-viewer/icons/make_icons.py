"""Draw the app icons: a slot canyon with a rope. Run inside `nix develop` (needs Pillow):

    python web/canyon-viewer/icons/make_icons.py

Writes icon-192.png, icon-512.png, icon-maskable-512.png and apple-touch-icon.png (180x180,
opaque: iOS paints transparent pixels black) next to this script.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BACKGROUND = (47, 111, 78)  # --accent in style.css
WALL = (27, 67, 50)
ROPE = (250, 248, 240)
SCALE = 4  # draw big, then shrink for smooth edges


def glyph(size: int) -> Image.Image:
    """The canyon on a transparent square of `size` pixels."""
    big = size * SCALE
    unit = big / 512
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def pts(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * unit, y * unit) for x, y in points]

    draw.polygon(
        pts([(0, 150), (190, 150), (215, 250), (185, 330), (225, 420), (200, 512), (0, 512)]),
        fill=WALL,
    )
    draw.polygon(
        pts([(512, 120), (322, 120), (300, 230), (335, 320), (295, 410), (320, 512), (512, 512)]),
        fill=WALL,
    )
    draw.line(pts([(256, 98), (256, 380)]), fill=ROPE, width=round(14 * unit))
    draw.ellipse(pts([(228, 42), (284, 98)]), outline=ROPE, width=round(12 * unit))
    draw.ellipse(pts([(236, 362), (276, 402)]), fill=ROPE)
    return img.resize((size, size), Image.LANCZOS)


def icon(size: int, inset: float = 1.0) -> Image.Image:
    """Opaque icon; `inset` < 1 shrinks the glyph into the safe zone of a maskable icon."""
    out = Image.new("RGB", (size, size), BACKGROUND)
    inner = round(size * inset)
    art = glyph(inner)
    offset = (size - inner) // 2
    out.paste(art, (offset, offset), art)
    return out


def main() -> None:
    icon(192).save(HERE / "icon-192.png", optimize=True)
    icon(512).save(HERE / "icon-512.png", optimize=True)
    icon(512, inset=0.8).save(HERE / "icon-maskable-512.png", optimize=True)
    icon(180).save(HERE / "apple-touch-icon.png", optimize=True)


if __name__ == "__main__":
    main()

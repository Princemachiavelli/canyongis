from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..ravine_compare import OVERLAY_DOWNSAMPLE_FACTOR, _downsample_by_factor


@dataclass(frozen=True)
class PreviewSpec:
    old_name: str
    new_name: str
    url_field: str


PREVIEWS: tuple[PreviewSpec, ...] = (
    PreviewSpec("panel_b_tpi.png", "panel_b_tpi.webp", "panel_b_tpi_url"),
    PreviewSpec(
        "flow_accum_log_preview.png", "flow_accum_log_preview.webp", "flow_preview_url"
    ),
    PreviewSpec(
        "stream_network_preview.png", "stream_network_preview.webp", "stream_preview_url"
    ),
)


def _convert_one(png_path: Path, new_name: str) -> tuple[int, int]:
    img = Image.open(png_path)
    img.load()
    out = png_path.with_name(new_name)
    _downsample_by_factor(img, OVERLAY_DOWNSAMPLE_FACTOR).save(out, quality=85, method=4)
    return png_path.stat().st_size, out.stat().st_size


def migrate(output_root: Path, *, delete_old: bool = False) -> None:
    converted = 0
    total_old = 0
    total_new = 0
    for spec in PREVIEWS:
        for png in output_root.rglob(spec.old_name):
            old_size, new_size = _convert_one(png, spec.new_name)
            total_old += old_size
            total_new += new_size
            converted += 1
            print(
                f"{png.relative_to(output_root)} → {spec.new_name}  "
                f"{old_size // 1024} KB → {new_size // 1024} KB"
            )
            if delete_old:
                png.unlink()

    jobs_path = output_root / "jobs.json"
    if jobs_path.exists():
        rows = json.loads(jobs_path.read_text())
        changed = 0
        for row in rows:
            result = row.get("result") or {}
            for spec in PREVIEWS:
                url = result.get(spec.url_field)
                if isinstance(url, str) and url.endswith(f"/{spec.old_name}"):
                    result[spec.url_field] = url[: -len(spec.old_name)] + spec.new_name
                    changed += 1
        if changed:
            jobs_path.write_text(json.dumps(rows, indent=2) + "\n")
            print(f"updated {changed} jobs.json entries")

    saved_kb = (total_old - total_new) // 1024
    print(f"converted {converted} files, saved {saved_kb} KB")


def cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-web-migrate-previews",
        description="Convert legacy .png overlays to downscaled WebP.",
    )
    p.add_argument(
        "--output-root", default="runs/web", help="Web output root (default: runs/web)."
    )
    p.add_argument(
        "--delete-old", action="store_true", help="Delete original .png after conversion."
    )
    args = p.parse_args(argv)
    migrate(Path(args.output_root), delete_old=args.delete_old)


if __name__ == "__main__":
    cli()

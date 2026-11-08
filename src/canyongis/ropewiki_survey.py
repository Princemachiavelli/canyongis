"""Survey Ropewiki for technical canyons whose KMLs already mark rappel locations.

Technical-section labels (``canyongis-reach-eval --technical markers``) need KMLs with
named rappel points (R1, R2 30 ft, rap 3, Final rappel, ...). Most KMLs have none, so
rather than placing our own, this finds the canyons whose Ropewiki KML already has
them:

1. One paged SMW query for technical (rating 3/4) canyons in a bbox: KML URL, rappel
   count, longest rappel.
2. Download each KML once (cached, throttled) and locate its technical section.
3. Write ``survey.json`` / ``survey.csv`` plus ``marked_not_in_manifest.txt``: page
   URLs ready for ``canyongis-ropewiki-sync``, which fetches the DEM tiles.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

from .ingest import ROPEWIKI_PAGE_URL, query_ropewiki_canyon_props
from .io import log
from .reach_eval import download_ropewiki_kml, kml_title, technical_section

# Southern California (San Gabriels, San Bernardinos, Santa Monicas, Anza-Borrego...).
DEFAULT_BBOX = (-121.0, 32.5, -114.0, 35.8)


def _manifest_titles(manifest_paths: list[Path]) -> set[str]:
    titles: set[str] = set()
    for m in manifest_paths:
        for e in json.loads(m.read_text()):
            for k in e.get("kml_paths", [e.get("kml_path")]):
                if k:
                    titles.add(kml_title(k))
    return titles


def survey_ropewiki(
    out_dir: Path,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    *,
    manifest_paths: list[Path] | None = None,
    margin_m: float = 200.0,
    delay_s: float = 0.5,
) -> list[dict[str, Any]]:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    props = query_ropewiki_canyon_props(
        bounds_wgs84=bbox, conditions="[[Has technical rating::3||4]]"
    )
    log(f"ropewiki-survey: {len(props)} technical canyons in {bbox}")
    in_manifest = _manifest_titles(manifest_paths or [])

    rows: list[dict[str, Any]] = []
    for title in sorted(props):
        p = props[title]
        url = p.get("Has KML file")
        row: dict[str, Any] = {
            "title": title,
            "page_url": ROPEWIKI_PAGE_URL.format(title=title.replace(" ", "_")),
            "technical": p.get("Has technical rating"),
            "ropewiki_rappels": p.get("Has number of rappels"),
            "longest_rappel_m": p.get("Has longest rappel"),
            "lat": p.get("Has latitude"),
            "lon": p.get("Has longitude"),
            "in_manifest": title in in_manifest,
            "has_kml": False,
            "rappel_points": 0,
            "marked": False,
            "section_km": 0.0,
            "descent_km": 0.0,
            "kml_path": None,
        }
        if isinstance(url, str) and url.startswith("http"):
            before = time.monotonic()
            kml = download_ropewiki_kml(url, out_dir)
            if kml is not None:
                n_rap = row["ropewiki_rappels"]
                sec = technical_section(
                    kml, n_rap if isinstance(n_rap, float) else None, margin_m=margin_m
                )
                row.update(
                    has_kml=True,
                    rappel_points=sec.n_anchors,
                    marked=sec.lines is not None,
                    section_km=round(sec.section_km, 3),
                    descent_km=round(sec.full_km, 3),
                    kml_path=str(kml),
                )
            # Throttle only real downloads (cached files return almost instantly).
            if time.monotonic() - before > 0.05:
                time.sleep(delay_s)
        rows.append(row)

    (out_dir / "survey.json").write_text(json.dumps(rows, indent=1))
    with (out_dir / "survey.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["title"])
        writer.writeheader()
        writer.writerows(rows)
    new_marked = [r for r in rows if r["marked"] and not r["in_manifest"]]
    (out_dir / "marked_not_in_manifest.txt").write_text(
        "".join(f"{r['page_url']}\n" for r in new_marked)
    )

    with_kml = [r for r in rows if r["has_kml"]]
    marked = [r for r in rows if r["marked"]]
    log(
        f"ropewiki-survey: {len(with_kml)} with a usable KML, {len(marked)} with marked "
        f"rappels ({sum(r['section_km'] for r in marked):.1f} technical km); "
        f"{len(new_marked)} marked canyons not in the manifest → "
        f"{out_dir / 'marked_not_in_manifest.txt'}"
    )
    return rows


def ropewiki_survey_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ropewiki-survey",
        description="Find Ropewiki technical canyons whose KMLs mark rappel locations.",
    )
    p.add_argument("out_dir", type=Path)
    p.add_argument(
        "--bbox",
        default=",".join(f"{v:g}" for v in DEFAULT_BBOX),
        help="west,south,east,north in degrees (default: Southern California).",
    )
    p.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help="Training manifest(s); canyons already in them are flagged. Repeatable.",
    )
    p.add_argument("--margin-m", type=float, default=200.0)
    p.add_argument("--delay-s", type=float, default=0.5, help="Pause between KML downloads.")
    a = p.parse_args(argv)
    bbox = tuple(float(v) for v in a.bbox.split(","))
    if len(bbox) != 4:
        p.error("--bbox expects west,south,east,north")
    survey_ropewiki(
        a.out_dir, bbox, manifest_paths=a.manifest, margin_m=a.margin_m, delay_s=a.delay_s
    )


if __name__ == "__main__":
    ropewiki_survey_cli()

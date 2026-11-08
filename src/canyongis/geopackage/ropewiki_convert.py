"""Convert Ropewiki canyon pages into the canyon GeoPackage schema.

For each page this reads the ``{{Canyon}}`` infobox, prose sections, rappel lines,
beta links, ``Conditions:`` and ``Votes:`` subpages, and the page's KML (routes,
rappel anchors, waypoints). Rows are replaced per canyon, so re-running on a page
updates it in place.

Usage::

    canyongis-ropewiki-gpkg canyons.gpkg "Lytle Creek (Middle Fork)" \\
        "https://ropewiki.com/Willow_Creek_Canyon" --cache-dir runs/ropewiki-gpkg
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..io import log
from .geometry import linestring_blob, point_blob
from .schema import create_geopackage, open_geopackage, refresh_extents
from .wikitext import (
    Rappel,
    external_links,
    find_template,
    first_int,
    first_number,
    parse_coded,
    parse_comments,
    parse_file_list,
    rappels,
    split_sections,
    strip_template,
    wiki_to_html,
    wiki_to_text,
)

ROPEWIKI_PAGE_URL = "https://ropewiki.com/{title}"
FT_TO_M = 0.3048
MI_TO_KM = 1.609344
NS_CONDITIONS = 190
NS_VOTES = 196
_BATCH = 50
_ANCHOR_NUMBER = re.compile(r"(?i)\b(?:r|rap(?:pel)?s?)\s*#?\s*(\d+)\b")
_SKIP_SECTIONS = {"beta sites", "trip reports and media"}
_WAYPOINT_KINDS = (
    ("parking", "parking"),
    ("start", "start"),
    ("end", "end"),
    ("checkpoint", "checkpoint"),
)


@dataclass
class CanyonRow:
    ropewiki_title: str
    ropewiki_page_id: int | None
    revision_id: int | None
    template: dict[str, str]
    sections: list[tuple[str, str]]
    rappels: list[Rappel]
    links: list[tuple[str, str]]
    conditions: list[dict[str, Any]] = field(default_factory=list)
    ratings: list[dict[str, Any]] = field(default_factory=list)
    kmls: list[dict[str, Any]] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Ropewiki access (reuses the canyongis ingest helpers and User-Agent)
# -----------------------------------------------------------------------------


def _query(**params: Any) -> dict[str, Any]:
    from ..ingest import _ropewiki_query

    return _ropewiki_query(**params)


def fetch_pages(titles: list[str]) -> dict[str, dict[str, Any]]:
    """``{title: {pageid, revid, text}}`` for existing pages. Missing pages are omitted."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(titles), _BATCH):
        data = _query(
            action="query",
            prop="revisions",
            rvprop="content|ids|timestamp",
            rvslots="main",
            titles="|".join(titles[i : i + _BATCH]),
        )
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            rev = page["revisions"][0]
            out[page["title"]] = {
                "pageid": page.get("pageid"),
                "revid": rev.get("revid"),
                "timestamp": rev.get("timestamp"),
                "text": rev["slots"]["main"]["content"],
            }
    return out


def list_subpages(namespace: int, prefix: str) -> list[str]:
    titles: list[str] = []
    cont: dict[str, str] = {}
    while True:
        data = _query(
            action="query",
            list="allpages",
            apnamespace=namespace,
            apprefix=prefix,
            aplimit=500,
            **cont,
        )
        titles += [p["title"] for p in data["query"]["allpages"]]
        if "continue" not in data:
            return titles
        cont = {"apcontinue": data["continue"]["apcontinue"]}


def kml_urls(titles: list[str]) -> dict[str, str]:
    """``{title: KML URL}`` from the page's ``Has KML file`` semantic property."""
    from ..ingest import query_ropewiki_canyon_props

    props = query_ropewiki_canyon_props(titles=titles, props=("Has KML file",))
    out: dict[str, str] = {}
    for title, values in props.items():
        url = values.get("Has KML file")
        if isinstance(url, str) and url.startswith("http"):
            out[title.replace("_", " ")] = url
    return out


# -----------------------------------------------------------------------------
# Page -> row
# -----------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    return value or None


def _yes_no(value: str | None) -> int | None:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in {"yes", "y", "true"}:
        return 1
    if lowered in {"no", "n", "false"}:
        return 0
    return None


def _coords(value: str | None) -> tuple[float, float] | None:
    """Ropewiki stores ``lat, lon``. Returns ``(lon, lat)`` for GeoPackage points."""
    if not value:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lon, lat


def _iso_date(value: str | None) -> str | None:
    """``2025/02/22`` -> ``2025-02-22``. Unparseable values are kept as written."""
    if not value:
        return None
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value.strip())
    if not m:
        return value.strip()
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def build_row(title: str, page: dict[str, Any], tpl: dict[str, str]) -> CanyonRow:
    body = strip_template(page["text"], "Canyon")
    sections = split_sections(body)
    rappel_seen: dict[str, Rappel] = {}
    links: list[tuple[str, str]] = []
    for heading, section_body in sections:
        if heading.lower() in _SKIP_SECTIONS:
            if heading.lower() == "beta sites":
                links = external_links(section_body)
            continue
        for r in rappels(section_body):
            rappel_seen.setdefault(r.label, r)
    return CanyonRow(
        ropewiki_title=title,
        ropewiki_page_id=page.get("pageid"),
        revision_id=page.get("revid"),
        template=tpl,
        sections=sections,
        rappels=list(rappel_seen.values()),
        links=links,
    )


_TITLE_STAMP = re.compile(r"-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


def _title_stamp(title: str) -> str | None:
    """Condition page names end in ``-YYYYMMDDHHMMSS`` (when the report was filed)."""
    m = _TITLE_STAMP.search(title)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(g) for g in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{sec:02d}"


def condition_record(source_title: str, page: dict[str, Any]) -> dict[str, Any] | None:
    """One ``Conditions:`` report as structured columns, cleaned notes and photo names."""
    tpl = find_template(page["text"], "Condition")
    if tpl is None:
        return None
    general_wiki, notes_wiki = parse_comments(tpl.get("Comments", ""))
    q_code, q_label, _ = parse_coded(tpl.get("Quality condition"))
    w_code, w_label, w_note = parse_coded(tpl.get("Water condition"))
    s_code, s_label, s_note = parse_coded(tpl.get("Wetsuit condition"))
    d_code, d_label, d_note = parse_coded(tpl.get("Difficulty condition"))
    reported_at = _title_stamp(source_title)
    trip_date = _iso_date(tpl.get("Date"))
    trip_date_source = "date_field" if trip_date else None
    if trip_date is None and reported_at:
        # Some reports omit Date; the filing date is the best available stand-in.
        trip_date = reported_at[:10]
        trip_date_source = "filed_date"
    notes: list[tuple[str, str]] = []
    for labels, note_wiki in notes_wiki:
        text = wiki_to_text(note_wiki)
        notes += [(label, text) for label in labels]
    return {
        "source_title": source_title,
        "trip_date": trip_date,
        "trip_date_source": trip_date_source,
        "reported_by": _clean(tpl.get("ReportedBy")),
        "reported_at": reported_at,
        "revised_at": page.get("timestamp"),
        "revision_id": page.get("revid"),
        "location_text": _clean(tpl.get("Location")),
        "quality": (q_code, q_label),
        "water": (w_code, w_label, w_note),
        "wetsuit": (s_code, s_label, s_note),
        "difficulty": (d_code, d_label, d_note),
        "team_size": first_int(tpl.get("Team size")),
        # Team time is written in hours ("5.5"); the wiki has no unit field.
        "team_time_h": first_number(tpl.get("Team time")),
        "team_min_experience": _clean(tpl.get("Team min experience")),
        "team_max_experience": _clean(tpl.get("Team max experience")),
        "general_notes": wiki_to_text(general_wiki) or None,
        "rappel_notes": notes,
        "photos": parse_file_list(tpl.get("FileList")),
        "raw_template_json": json.dumps(tpl, ensure_ascii=False, sort_keys=True),
    }


def rating_record(source_title: str, page: dict[str, Any]) -> dict[str, Any] | None:
    tpl = find_template(page["text"], "Page rating")
    if tpl is None:
        return None
    return {
        "source_title": source_title,
        "rater": _clean(tpl.get("User")),
        "rating": first_int(tpl.get("Rating")),
    }


# -----------------------------------------------------------------------------
# KML -> geometry
# -----------------------------------------------------------------------------


def _anchor_key(name: str) -> str | None:
    m = _ANCHOR_NUMBER.search(name)
    return m.group(1) if m else None


def _waypoint_kind(name: str) -> str:
    lowered = name.lower()
    for token, kind in _WAYPOINT_KINDS:
        if token in lowered:
            return kind
    return "other"


def _route_kind(name: str, category: str) -> str:
    if "exit" in name.lower():
        return "exit"
    return {"descent": "descent", "approach": "approach"}.get(category, "unknown")


def _download_kml(url: str, cache_dir: Path) -> Path | None:
    from ..reach_eval import download_ropewiki_kml

    return download_ropewiki_kml(url, cache_dir)


def parse_kml_geometry(path: Path) -> dict[str, Any]:
    from pyproj import Geod

    from ..validate import parse_kml

    feats = parse_kml(path)
    geod = Geod(ellps="WGS84")
    routes: list[dict[str, Any]] = []
    for gdf in (feats.descent_lines, feats.approach_lines, feats.unclassified_lines):
        for _, row in gdf.iterrows():
            coords = list(row.geometry.coords)
            routes.append(
                {
                    "kind": _route_kind(row["name"], row["category"]),
                    "name": row["name"],
                    "coords": coords,
                    "length_m": _polyline_length(geod, coords),
                }
            )
    anchors = []
    for _, row in feats.anchor_points().iterrows():
        anchors.append({"name": row["name"], "key": _anchor_key(row["name"]),
                        "lon": row.geometry.x, "lat": row.geometry.y})
    waypoints = []
    anchor_names = {a["name"] for a in anchors}
    for _, row in feats.points.iterrows():
        if row["name"] in anchor_names:
            continue
        waypoints.append({"kind": _waypoint_kind(row["name"]), "name": row["name"],
                          "lon": row.geometry.x, "lat": row.geometry.y})
    return {"routes": routes, "anchors": anchors, "waypoints": waypoints}


def _polyline_length(geod: Any, coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:], strict=False):
        total += geod.inv(x1, y1, x2, y2)[2]
    return round(total, 1)


# -----------------------------------------------------------------------------
# Row -> GeoPackage
# -----------------------------------------------------------------------------


def _replace_canyon(conn: sqlite3.Connection, row: CanyonRow, source_url: str,
                    geo: dict[str, Any] | None) -> None:
    conn.execute("DELETE FROM canyons WHERE ropewiki_title = ?", (row.ropewiki_title,))
    tpl = row.template
    coords = _coords(tpl.get("Coordinates"))
    fetched = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.execute(
        """INSERT INTO canyons (
            ropewiki_title, ropewiki_page_id, also_known_as, region, technical_rating,
            water_rating, time_rating, number_of_rappels, longest_rappel_m, hike_length_km,
            approach_gain_m, exit_gain_m, fastest_time_h, slowest_time_h, permits,
            best_season, vehicle, shuttle, explored, raw_template_json, source_url,
            source_revision_id, fetched_at, geom)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row.ropewiki_title,
            row.ropewiki_page_id,
            _clean(tpl.get("AKA")),
            _clean(tpl.get("Region")),
            first_int(tpl.get("Technical rating")),
            _clean(tpl.get("Water rating")),
            _clean(tpl.get("Time rating")),
            first_int(tpl.get("Number of rappels")),
            _scaled(tpl.get("Longest rappel"), FT_TO_M),
            _scaled(tpl.get("Hike length"), MI_TO_KM),
            _scaled(tpl.get("Approach elevation gain"), FT_TO_M),
            _scaled(tpl.get("Exit elevation gain"), FT_TO_M),
            first_number(tpl.get("Fastest typical time")),
            first_number(tpl.get("Slowest typical time")),
            _clean(tpl.get("Permits")),
            _clean(tpl.get("Best season")),
            _clean(tpl.get("Vehicle")),
            first_int(tpl.get("Shuttle")),
            _yes_no(tpl.get("Explored")),
            json.dumps(tpl, ensure_ascii=False, sort_keys=True),
            source_url,
            row.revision_id,
            fetched,
            point_blob(*coords) if coords else None,
        ),
    )
    canyon_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO beta (canyon_id, position, heading, body_wiki, body_text, body_html)"
        " VALUES (?,?,?,?,?,?)",
        [
            (canyon_id, i, h, b, wiki_to_text(b), wiki_to_html(b))
            for i, (h, b) in enumerate(row.sections)
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO links (canyon_id, label, url, source_name)"
        " VALUES (?,?,?,?)",
        [(canyon_id, label or None, url, urlparse(url).netloc) for url, label in row.links],
    )
    kml_ids = _insert_kmls(conn, canyon_id, row.kmls)
    _insert_geometry(conn, canyon_id, row, geo, kml_ids)
    for c in row.conditions:
        _insert_condition(conn, canyon_id, c)
    for r in row.ratings:
        conn.execute(
            "INSERT OR REPLACE INTO ratings (canyon_id, source_title, rater, rating)"
            " VALUES (?,?,?,?)",
            (canyon_id, r["source_title"], r["rater"], r["rating"]),
        )


def _insert_condition(conn: sqlite3.Connection, canyon_id: int, c: dict[str, Any]) -> None:
    (q_code, q_label) = c["quality"]
    (w_code, w_label, w_note) = c["water"]
    (s_code, s_label, s_note) = c["wetsuit"]
    (d_code, d_label, d_note) = c["difficulty"]
    cur = conn.execute(
        """INSERT INTO conditions (
            canyon_id, source_title, trip_date, trip_date_source, reported_by, reported_at,
            revised_at, revision_id, location_text, quality_code, quality_label, water_code, water_label,
            water_note, wetsuit_code, wetsuit_label, wetsuit_note, difficulty_code,
            difficulty_label, difficulty_note, team_size, team_time_h, team_min_experience,
            team_max_experience, general_notes, source_url, raw_template_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            canyon_id, c["source_title"], c["trip_date"], c["trip_date_source"],
            c["reported_by"], c["reported_at"],
            c["revised_at"], c["revision_id"], c["location_text"], q_code, q_label, w_code,
            w_label, w_note, s_code, s_label, s_note, d_code, d_label, d_note, c["team_size"],
            c["team_time_h"], c["team_min_experience"], c["team_max_experience"],
            c["general_notes"], ROPEWIKI_PAGE_URL.format(title=c["source_title"].replace(" ", "_")),
            c["raw_template_json"],
        ),
    )
    report_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO condition_rappel_notes (report_id, rappel_label, note) VALUES (?,?,?)",
        [(report_id, label, note) for label, note in c["rappel_notes"]],
    )
    conn.executemany(
        "INSERT INTO condition_photos (report_id, sequence, filename) VALUES (?,?,?)",
        [(report_id, i, name) for i, name in enumerate(c["photos"])],
    )


def _scaled(value: str | None, factor: float) -> float | None:
    number = first_number(value)
    return round(number * factor, 2) if number is not None else None


def _insert_kmls(conn: sqlite3.Connection, canyon_id: int,
                 kmls: list[dict[str, Any]]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for k in kmls:
        cur = conn.execute(
            "INSERT INTO kml_files (canyon_id, file_title, url, sha256, downloaded_at)"
            " VALUES (?,?,?,?,?)",
            (canyon_id, k["file_title"], k["url"], k["sha256"], k["downloaded_at"]),
        )
        ids[k["file_title"]] = cur.lastrowid
    return ids


def _insert_geometry(conn: sqlite3.Connection, canyon_id: int, row: CanyonRow,
                     geo: dict[str, Any] | None, kml_ids: dict[str, int]) -> None:
    if geo is None:
        geo = {"routes": [], "anchors": [], "waypoints": []}
    kml_id = next(iter(kml_ids.values()), None)
    for r in geo["routes"]:
        conn.execute(
            "INSERT INTO routes (canyon_id, kind, name, kml_file_id, length_m, geom)"
            " VALUES (?,?,?,?,?,?)",
            (canyon_id, r["kind"], r["name"], kml_id, r["length_m"],
             linestring_blob(r["coords"])),
        )
    for w in geo["waypoints"]:
        conn.execute(
            "INSERT INTO waypoints (canyon_id, kind, name, kml_file_id, geom) VALUES (?,?,?,?,?)",
            (canyon_id, w["kind"], w["name"], kml_id, point_blob(w["lon"], w["lat"])),
        )
    # Rappels: wiki rappel lines, each matched to a KML anchor by number. Anchors that
    # appear only in the KML are added after, so nothing is dropped.
    anchors_by_key = {a["key"]: a for a in geo["anchors"] if a["key"]}
    used: set[str] = set()
    sequence = 0
    for r in row.rappels:
        sequence += 1
        key = re.sub(r"\D", "", r.label)
        anchor = anchors_by_key.get(key)
        if anchor:
            used.add(anchor["name"])
        conn.execute(
            "INSERT INTO rappels (canyon_id, sequence, label, height_m, anchor_text, notes,"
            " kml_file_id, geom) VALUES (?,?,?,?,?,?,?,?)",
            (canyon_id, sequence, r.label, r.height_m, None, wiki_to_text(r.text), kml_id,
             point_blob(anchor["lon"], anchor["lat"]) if anchor else None),
        )
    for a in geo["anchors"]:
        if a["name"] in used:
            continue
        sequence += 1
        conn.execute(
            "INSERT INTO rappels (canyon_id, sequence, label, height_m, anchor_text, notes,"
            " kml_file_id, geom) VALUES (?,?,?,?,?,?,?,?)",
            (canyon_id, sequence, a["name"], None, None, None, kml_id,
             point_blob(a["lon"], a["lat"])),
        )


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def _load_kmls(titles: list[str], cache_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    urls = kml_urls(titles)
    for title, url in urls.items():
        path = _download_kml(url, cache_dir)
        if path is None:
            log(f"no usable KML for {title}")
            continue
        out[title] = {
            "path": path,
            "file_title": path.name,
            "url": url,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "downloaded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    return out


def convert(gpkg_path: str | Path, titles: list[str], cache_dir: str | Path,
            fetch_kml: bool = True) -> list[str]:
    """Write or update ``gpkg_path`` with the given Ropewiki page titles.

    Returns the titles that were stored. Pages that are missing or lack a
    ``{{Canyon}}`` infobox are logged and skipped.
    """
    cache = Path(cache_dir)
    gpkg = Path(gpkg_path)
    conn = open_geopackage(gpkg) if gpkg.exists() else create_geopackage(gpkg)
    pages = fetch_pages(titles)
    stored: list[str] = []
    kmls = _load_kmls(list(pages), cache) if fetch_kml else {}
    for title in titles:
        page = pages.get(title)
        if page is None:
            log(f"Ropewiki page not found: {title}")
            continue
        tpl = find_template(page["text"], "Canyon")
        if tpl is None:
            log(f"not a canyon page (no {{{{Canyon}}}}): {title}")
            continue
        row = build_row(title, page, tpl)
        row.conditions = _subpage_records(title, NS_CONDITIONS, condition_record)
        row.ratings = _subpage_records(title, NS_VOTES, rating_record)
        geo = None
        if title in kmls:
            kml = kmls[title]
            row.kmls = [kml]
            geo = parse_kml_geometry(kml["path"])
        _replace_canyon(conn, row, ROPEWIKI_PAGE_URL.format(title=title.replace(" ", "_")), geo)
        conn.commit()
        stored.append(title)
        log(f"stored {title}: {len(row.rappels)} rappels, {len(row.conditions)} conditions, "
            f"{len(row.ratings)} ratings, {'KML' if geo else 'no KML'}")
    refresh_extents(conn)
    conn.close()
    return stored


def _subpage_records(title: str, namespace: int, parse) -> list[dict[str, Any]]:
    """Fetch and parse ``Conditions:<title>-*`` or ``Votes:<title>/*`` subpages."""
    separator = "-" if namespace == NS_CONDITIONS else "/"
    subpages = list_subpages(namespace, f"{title}{separator}")
    if not subpages:
        return []
    texts = fetch_pages(subpages)
    records = []
    for sub in subpages:
        page = texts.get(sub)
        if page is None:
            continue
        rec = parse(sub, page)
        if rec is not None:
            records.append(rec)
    return records


def convert_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="canyongis-ropewiki-gpkg",
        description="Convert Ropewiki canyon pages into the canyon GeoPackage schema.",
    )
    parser.add_argument("gpkg", help="output GeoPackage (created if missing, updated otherwise)")
    parser.add_argument("pages", nargs="+", help="Ropewiki page titles or URLs")
    parser.add_argument("--cache-dir", default="runs/ropewiki-gpkg/cache")
    parser.add_argument("--no-kml", action="store_true", help="skip KML download and geometry")
    args = parser.parse_args(argv)

    from ..ingest import normalize_ropewiki_title

    titles = [normalize_ropewiki_title(p).replace("_", " ") for p in args.pages]
    stored = convert(args.gpkg, titles, args.cache_dir, fetch_kml=not args.no_kml)
    print(f"stored {len(stored)} of {len(titles)} pages in {args.gpkg}")


if __name__ == "__main__":
    convert_cli()

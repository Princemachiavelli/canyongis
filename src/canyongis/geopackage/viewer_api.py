"""Server side of the viewer's "get a canyon" feature.

The PWA searches Ropewiki through this module, asks for a canyon to be prepared, polls the
job, and downloads the finished GeoPackage into its own storage.

* **Search**: a live Semantic MediaWiki query on Ropewiki's canyon pages, by words in the title
  (case-insensitive: SMW wildcards are case-sensitive, so each word is sent in four spellings).
* **Data** lives in PostGIS (``pgstore``). A canyon is ingested from Ropewiki when it is not
  stored yet or its data is over 24 hours old; its weather forecast is refreshed when over
  3 hours old.
* **Map tiles** (imagery, hillshade, topo, contours) are built once per canyon into a tile file
  and reused by every export. They need US data (NAIP, USGS 1 m DEM); elsewhere the download
  is data only and the viewer falls back to its online map.
* **Download**: every request ends with a fresh export of the stored rows (on top of the tile
  file), so a download never carries old data.

One build runs at a time; asking for a canyon that is already being built joins that job.

**Updates**: a file the client already has can be brought up to date without downloading it
again. The client reads the fingerprints stored in its file (``section_stamps``), the server
compares them with its own (``/api/manifest``) and, on ``/api/updates``, refreshes the canyon
from Ropewiki if its data is over 24 hours old and returns just the sections that differ, as a
JSON delta the client applies in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from ..io import log
from . import pgstore, tiles

MAX_AGE = timedelta(hours=24)
# "Re-check Ropewiki now" skips the 24 hour rule, but not more often than this per canyon.
FORCE_MIN_AGE = timedelta(minutes=5)
SEARCH_LIMIT = 20
SEARCH_FETCH_LIMIT = 60
SEARCH_CACHE_SECONDS = 600
MAX_QUERY_WORDS = 5
MAX_BODY_BYTES = 4096
# Rebuilding map tiles is retried after this long (coverage data can change; a network error
# should not wait long).
TILE_RETRY = {"failed": timedelta(hours=1), "unavailable": timedelta(days=7)}
# Rebuilding an artifact's layers re-downloads the DEM, so it is not retried too eagerly: a layer
# the area never had (no aerial imagery, say) is looked for again after a month, a stale one
# whose rebuild failed after a day.
LAYER_MISSING_RETRY = timedelta(days=30)
LAYER_STALE_RETRY = timedelta(days=1)
LAYER_LABELS = {
    "tiles_osm": "Street map",
    "tiles_imagery": "Aerial imagery",
    "tiles_hillshade": "Hillshade",
    "tiles_contours_m": "Contours (m)",
    "tiles_contours_ft": "Contours (ft)",
    "tiles_terrain": "3D elevation",
}


def layers_to_rebuild(
    layers: dict[str, Any], attempts: dict[str, Any], now: datetime
) -> dict[str, str]:
    """The tile layers a canyon's artifact needs rebuilt, with why: ``missing`` (never built),
    ``version`` (built by older code, see tiles.LAYER_VERSIONS), ``age`` (over a year old) or
    ``unknown`` (no build time). Layers tried recently without success are left alone."""
    out: dict[str, str] = {}
    for table in tiles.LAYER_VERSIONS:
        held = layers.get(table)
        if held is None:
            why, window = "missing", LAYER_MISSING_RETRY
        else:
            why = tiles.layer_staleness(held.get("version"), held.get("built_at"), table, now)
            window = LAYER_STALE_RETRY
        if why is None:
            continue
        tried = pgstore._parse_time(attempts.get(table))
        if tried is None or now - tried >= window:
            out[table] = why
    return out


def tile_updates(server: dict[str, Any], have: dict[str, Any]) -> list[dict[str, str]]:
    """Tile layers the server holds that a client lacks or holds an older build of."""
    out = []
    for table, built in server.items():
        mine = have.get(table)
        theirs = pgstore._parse_time(built.get("built_at"))
        if mine is None:
            why = "missing"
        elif built.get("version", 1) > (mine.get("version") or 0):
            why = "version"
        else:
            held_at = pgstore._parse_time(mine.get("built_at"))
            if theirs is None or (held_at is not None and held_at >= theirs):
                continue
            why = "newer"
        out.append({"layer": table, "label": LAYER_LABELS.get(table, table), "reason": why})
    return out


def tile_stamps_of(path: Path) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(path)
    try:
        return tiles.read_tile_stamps(conn)
    finally:
        conn.close()


EXPORT_KEEP_SECONDS = 6 * 3600
TILE_TIMEOUT_SECONDS = 3 * 3600

_WORD = re.compile(r"[\w'.&-]+")
_TITLE_FORBIDDEN = re.compile(r"[\[\]{}|#<>\x00-\x1f]")


class BuildError(Exception):
    """A build failed in a way worth showing to the user as is."""


class TileUnavailable(Exception):
    """No offline map can be built for this canyon (not a failure of the build)."""


# -----------------------------------------------------------------------------
# search
# -----------------------------------------------------------------------------


def query_words(text: str) -> list[str]:
    """The words of a search box entry, restricted to characters that are safe in an SMW query
    (``]]``, ``|`` and the like could add conditions of their own)."""
    words = [w for w in _WORD.findall(text or "") if len(w) <= 40 and any(c.isalnum() for c in w)]
    words = words[:MAX_QUERY_WORDS]
    return words if sum(len(w) for w in words) >= 2 else []


def case_variants(word: str) -> list[str]:
    """SMW's page-name wildcards are case-sensitive, so ask for the likely spellings."""
    return list(dict.fromkeys([word, word.lower(), word.capitalize(), word.upper(), word.title()]))


def search_query(words: list[str], limit: int = SEARCH_FETCH_LIMIT) -> str:
    conditions = ["[[Category:Canyons]]"] + [
        "[[" + "||".join(f"~*{v}*" for v in case_variants(w)) + "]]" for w in words
    ]
    printouts = [
        "?Has latitude",
        "?Has longitude",
        "?Has region",
        "?Has technical rating",
        "?Has water rating",
        "?Has number of rappels",
    ]
    return " ".join(conditions) + "|" + "|".join([*printouts, f"limit={limit}"])


def _first(values: Any) -> Any:
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("fulltext") or value.get("raw") or value.get("value")
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_ask(query: str) -> dict[str, Any]:
    from ..ingest import _ropewiki_query

    try:
        return _ropewiki_query(action="ask", query=query)
    except ValueError as err:  # an HTML error page instead of JSON
        raise OSError(f"Ropewiki sent an unreadable answer ({err})") from err


def search_canyons(
    text: str,
    *,
    ask: Callable[[str], dict[str, Any]] = _default_ask,
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Ropewiki canyons whose title contains every word of ``text``, best matches first."""
    words = query_words(text)
    if not words:
        return []
    from ..ingest import _smw_results

    out = []
    for title, info in _smw_results(ask(search_query(words))):
        printouts = info.get("printouts") or {}
        rappels = _number(_first(printouts.get("Has number of rappels")))
        out.append(
            {
                "title": title,
                "region": _first(printouts.get("Has region")),
                "latitude": _number(_first(printouts.get("Has latitude"))),
                "longitude": _number(_first(printouts.get("Has longitude"))),
                "technical_rating": _first(printouts.get("Has technical rating")),
                "water_rating": _first(printouts.get("Has water rating")),
                "rappels": int(rappels) if rappels is not None else None,
            }
        )
    phrase = " ".join(words).lower()
    out.sort(key=lambda r: (not r["title"].lower().startswith(phrase), r["title"].lower()))
    return out[:limit]


def _default_verify(title: str) -> bool:
    from ..ingest import _ropewiki_query

    try:
        data = _ropewiki_query(
            action="query", titles=title, prop="categories", clcategories="Category:Canyons"
        )
    except ValueError as err:
        raise OSError(f"Ropewiki sent an unreadable answer ({err})") from err
    pages = data.get("query", {}).get("pages", [])
    return (
        bool(pages)
        and not pages[0].get("missing")
        and pages[0].get("title") == title
        and bool(pages[0].get("categories"))
    )


# -----------------------------------------------------------------------------
# tiles: planning and running the tile build
# -----------------------------------------------------------------------------


def default_plan_tiles(gpkg: Path) -> dict[str, int]:
    """Size up the map for a canyon file, or raise ``TileUnavailable`` when it cannot be built."""
    from . import data_fetch
    from .schema import open_geopackage
    from .tile_build import DEFAULT_BUFFER_M, DEFAULT_MAX_TILES, canyon_bbox_lonlat, plan
    from .tiles import buffer_bbox_lonlat

    conn = open_geopackage(gpkg)
    try:
        bbox = buffer_bbox_lonlat(canyon_bbox_lonlat(conn), DEFAULT_BUFFER_M)
    except ValueError:
        raise TileUnavailable(
            "This canyon has no location or track on Ropewiki, so there is no area to map."
        ) from None
    finally:
        conn.close()
    layers = ["osm", "imagery", "hillshade", "contours_m", "contours_ft", "terrain"]
    total = sum(sum(per_zoom.values()) for per_zoom in plan(bbox, layers).values())
    if total > DEFAULT_MAX_TILES:
        raise TileUnavailable(f"The area is too large for an offline map ({total} tiles).")
    dem = data_fetch.dem_products(bbox)
    if not dem:
        raise TileUnavailable(
            "No USGS 1 m elevation data covers this canyon (the offline map needs it), "
            "so only the canyon data is included."
        )
    return {
        "tiles": total,
        "dem_tiles": len(dem),
        "dem_bytes": data_fetch.total_bytes(dem),
        "naip_scenes": len(data_fetch.naip_scenes(bbox)),
    }


def run_command(cmd: list[str], on_line: Callable[[str], None], timeout: float) -> int:
    """Run a command (never through a shell), feeding its output lines to ``on_line``."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()
    finally:
        timer.cancel()


def default_ingest(title: str, out: Path, cache_dir: Path) -> None:
    """Fetch one canyon from Ropewiki into a scratch GeoPackage (no forecast: PostGIS has it)."""
    from .ropewiki_convert import convert

    if not convert(out, [title], cache_dir, fetch_kml=True, forecast=False):
        raise RuntimeError("the page could not be read as a canyon")


def slug_for(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()[:60] or "canyon"
    return f"{base}-{hashlib.sha1(title.encode()).hexdigest()[:8]}"


# -----------------------------------------------------------------------------
# build jobs
# -----------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    title: str
    want_tiles: bool = True
    kind: str = "build"  # build | update
    state: str = "queued"  # queued | running | done | failed
    stage: str = "Waiting for other builds"
    detail: str = ""
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    file: str | None = None
    size: int | None = None
    tiles: str = "none"  # included | unavailable | failed | skipped | none
    cached: bool = False
    # updates: the sections that differ from the client's, and the rows to bring it up to date
    have: dict[str, str] = field(default_factory=dict)
    have_tiles: dict[str, Any] | None = None  # tile layer -> {version, built_at} the client holds
    tiles_outdated: list[dict[str, str]] = field(default_factory=list)
    force: bool = False
    outdated: list[dict[str, str]] = field(default_factory=list)
    delta: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "stage": self.stage,
            "detail": self.detail,
            "error": self.error,
            "notes": list(self.notes),
            "file": self.file,
            "url": f"builds/{self.file}" if self.file else None,
            "size": self.size,
            "tiles": self.tiles,
            "cached": self.cached,
            "outdated": list(self.outdated),
            "tiles_outdated": list(self.tiles_outdated),
            "elapsed_s": round((self.finished or time.time()) - self.started),
        }


class BuildService:
    def __init__(
        self,
        root: str | Path,
        connect: Callable[[], Any],
        *,
        tiles: bool = True,
        max_age: timedelta = MAX_AGE,
        ask: Callable[[str], dict[str, Any]] = _default_ask,
        verify: Callable[[str], bool] = _default_verify,
        ingest: Callable[[str, Path, Path], None] = default_ingest,
        plan_tiles: Callable[[Path], dict[str, int]] = default_plan_tiles,
        runner: Callable[[list[str], Callable[[str], None], float], int] = run_command,
        fetch_forecast: Callable[[float, float], dict[str, Any]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        synchronous: bool = False,
    ) -> None:
        self.root = Path(root)
        self.exports = self.root / "exports"
        self.tile_files = self.root / "tiles"
        self.work = self.root / "work"
        # Raw 1 m DEM tiles, shared by every build (neighbouring canyons use the same ones), and
        # one cropped DEM per canyon, which is all a rebuild of its map layers needs. The raw tiles
        # are only ever deleted by a separate cleanup, once the cropped DEMs of the canyons that
        # use them exist.
        self.dem_sources = self.root / "dem_sources"
        self.dems = self.root / "dems"
        for directory in (self.exports, self.tile_files, self.work, self.dem_sources, self.dems):
            directory.mkdir(parents=True, exist_ok=True)
        self._connect = connect
        self.tiles_enabled = tiles
        self.max_age = max_age
        self._ask, self._verify, self._ingest = ask, verify, ingest
        self._plan_tiles, self._runner, self._fetch_forecast = plan_tiles, runner, fetch_forecast
        self._now = now
        self._synchronous = synchronous
        self._lock = threading.RLock()
        self._worker = threading.Lock()  # one build at a time
        self._jobs: dict[str, Job] = {}
        self._active: dict[tuple[str, str], Job] = {}
        self._search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ---- search -------------------------------------------------------------------

    def search(self, text: str) -> list[dict[str, Any]]:
        key = " ".join(query_words(text)).lower()
        if not key:
            return []
        with self._lock:
            hit = self._search_cache.get(key)
            fresh = hit is not None and time.time() - hit[0] < SEARCH_CACHE_SECONDS
            results = hit[1] if fresh else None
        if results is None:
            results = search_canyons(text, ask=self._ask)
            with self._lock:
                if len(self._search_cache) > 200:
                    self._search_cache.clear()
                self._search_cache[key] = (time.time(), results)
        return self._annotate(results)

    def _annotate(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Say which results are already prepared here (fresh data, map tiles)."""
        titles = [r["title"] for r in results]
        known: dict[str, datetime | None] = {}
        tiled: set[str] = set()
        try:
            with self._connect() as pg, pg.cursor() as cur:
                cur.execute(
                    "SELECT ropewiki_title, fetched_at FROM canyon.canyons"
                    " WHERE ropewiki_title = ANY(%s)",
                    (titles,),
                )
                known = {t: pgstore._parse_time(f) for t, f in cur.fetchall()}
                cur.execute(
                    "SELECT ropewiki_title FROM canyon.tile_builds"
                    " WHERE status = 'ready' AND ropewiki_title = ANY(%s)",
                    (titles,),
                )
                tiled = {t for (t,) in cur.fetchall()}
        except Exception as err:  # the database being down must not break search
            log(f"search annotations unavailable: {err}")
        now = self._now()
        return [
            {
                **r,
                "fresh": bool(known.get(r["title"])) and now - known[r["title"]] < self.max_age,
                "map_ready": r["title"] in tiled,
            }
            for r in results
        ]

    # ---- jobs ---------------------------------------------------------------------

    def request_build(self, title: str, *, want_tiles: bool = True) -> Job:
        if not title or len(title) > 200 or _TITLE_FORBIDDEN.search(title):
            raise ValueError("That is not a Ropewiki canyon title.")
        with self._lock:
            active = self._active.get(("build", title))
            if active:
                return active
        # A canyon already stored was checked when it was first ingested, so it can still be
        # served while Ropewiki is unreachable.
        if not self._is_stored(title) and not self._verify(title):
            raise ValueError(f"{title} is not a canyon page on Ropewiki.")
        with self._lock:
            active = self._active.get(("build", title))  # another request may have won the race
            if active:
                return active
            job = Job(
                id=uuid.uuid4().hex, title=title, want_tiles=want_tiles and self.tiles_enabled
            )
            self._jobs[job.id] = job
            self._active[("build", title)] = job
            self._prune()
        if self._synchronous:
            self._run(job)
        else:
            threading.Thread(
                target=self._run, args=(job,), daemon=True, name=f"build-{job.id[:6]}"
            ).start()
        return job

    def _is_stored(self, title: str) -> bool:
        try:
            with self._connect() as pg, pg.cursor() as cur:
                cur.execute("SELECT 1 FROM canyon.canyons WHERE ropewiki_title = %s", (title,))
                return cur.fetchone() is not None
        except Exception as err:  # a database problem shows up properly in the build itself
            log(f"could not look up {title}: {err}")
            return False

    def job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        for job_id in [i for i, j in self._jobs.items() if j.finished and j.finished < cutoff]:
            del self._jobs[job_id]
        for path in self.exports.glob("*.gpkg"):
            if time.time() - path.stat().st_mtime > EXPORT_KEEP_SECONDS:
                path.unlink(missing_ok=True)

    def _run(self, job: Job) -> None:
        with self._worker:
            job.state = "running"
            try:
                if job.kind == "update":
                    self._update_pipeline(job)
                    job.stage = (
                        "Update ready"
                        if job.outdated or job.tiles_outdated
                        else "Already up to date"
                    )
                else:
                    self._pipeline(job)
                    job.stage = "Ready to download"
                job.state = "done"
            except BuildError as err:
                job.state, job.error = "failed", str(err)
            except Exception as err:
                log("build failed:\n" + traceback.format_exc())
                job.state, job.error = "failed", f"Unexpected error: {err}"
            finally:
                job.finished = time.time()
                with self._lock:
                    self._active.pop((job.kind, job.title), None)

    def _say(self, job: Job, stage: str) -> None:
        job.stage, job.detail = stage, ""

    def _pipeline(self, job: Job) -> None:
        title, slug = job.title, slug_for(job.title)
        with self._connect() as pg:
            if not pgstore.schema_ready(pg):
                raise BuildError("The canyon database is not set up on the server.")
            self._ensure_data(job, pg, slug)
            self._say(job, "Updating the weather forecast")
            kwargs = {"fetch": self._fetch_forecast} if self._fetch_forecast else {}
            pgstore.refresh_forecast(pg, title, **kwargs)
            base = self._tile_base(job, pg, slug) if job.want_tiles else None
            if not job.want_tiles:
                job.tiles = "skipped"
            self._say(job, "Preparing the download")
            out = self.exports / f"{slug}.gpkg"
            pgstore.export_geopackage(pg, [title], out, base=base)
        job.file, job.size = out.name, out.stat().st_size

    # ---- updates ------------------------------------------------------------------

    def manifest(self, title: str) -> dict[str, Any] | None:
        """What the server holds for a canyon (None if it is not stored)."""
        with self._connect() as pg:
            if not pgstore.schema_ready(pg):
                raise BuildError("The canyon database is not set up on the server.")
            info = pgstore.manifest(pg, title)
            if info and info["map"] and info["map"]["status"] == "ready":
                state = self._tile_state(pg, title)
                if state:
                    # what the next update will rebuild first (missing, old or over a year old)
                    info["map"]["layers"] = pgstore._layer_states(state[2], self._now())
                    info["map"]["pending"] = layers_to_rebuild(state[2], state[3], self._now())
            return info

    def request_update(
        self,
        title: str,
        have: dict[str, str],
        *,
        force: bool = False,
        have_tiles: dict[str, Any] | None = None,
    ) -> Job:
        """Bring a client's copy of a canyon up to date: refresh the canyon from Ropewiki when
        its data is over 24 hours old, then work out which sections differ from ``have``
        (section -> fingerprint the client holds) and prepare their rows. With ``force`` the
        24 hour rule is skipped (Ropewiki is still not asked more than every 5 minutes)."""
        if not title or len(title) > 200 or _TITLE_FORBIDDEN.search(title):
            raise ValueError("That is not a Ropewiki canyon title.")
        have = {k: v for k, v in have.items() if k in pgstore.SECTIONS and isinstance(v, str)}
        with self._lock:
            active = self._active.get(("update", title))
            if (
                active
                and active.have == have
                and active.force == force
                and active.have_tiles == have_tiles
            ):
                return active
        if not self._is_stored(title) and not self._verify(title):
            raise ValueError(f"{title} is not a canyon page on Ropewiki.")
        with self._lock:
            job = Job(
                id=uuid.uuid4().hex,
                title=title,
                kind="update",
                have=have,
                have_tiles=have_tiles,
                force=force,
                want_tiles=False,
            )
            self._jobs[job.id] = job
            self._active[("update", title)] = job
            self._prune()
        if self._synchronous:
            self._run(job)
        else:
            threading.Thread(
                target=self._run, args=(job,), daemon=True, name=f"update-{job.id[:6]}"
            ).start()
        return job

    def _update_pipeline(self, job: Job) -> None:
        slug = slug_for(job.title)
        with self._connect() as pg:
            if not pgstore.schema_ready(pg):
                raise BuildError("The canyon database is not set up on the server.")
            self._ensure_data(job, pg, slug)
            if self.tiles_enabled:  # whatever the client sent: the server's map may be stale
                self._refresh_tiles(job, pg, slug)
            self._say(job, "Comparing with this device")
            info = pgstore.manifest(pg, job.title)
            if info is None:
                raise BuildError(f"{job.title} is not stored on the server.")
            names = pgstore.outdated_sections(info, job.have)
            job.outdated = [
                {
                    "section": n,
                    "label": info["sections"][n]["label"],
                    "updated_at": info["sections"][n]["updated_at"],
                }
                for n in names
            ]
            job.delta = pgstore.section_delta(pg, job.title, names) if names else None
            if job.have_tiles is not None:
                state = self._tile_state(pg, job.title)
                if state and state[0] == "ready":
                    job.tiles_outdated = tile_updates(state[2], job.have_tiles)

    def delta(self, job_id: str) -> dict[str, Any] | None:
        job = self.job(job_id)
        if job is None or job.kind != "update" or job.state != "done":
            return None
        return job.delta or {
            "title": job.title,
            "schema_version": pgstore.SCHEMA_VERSION,
            "sections": {},
        }

    # ---- Ropewiki data ------------------------------------------------------------

    def _ensure_data(self, job: Job, pg: Any, slug: str) -> None:
        fetched = pgstore.canyon_fetched_at(pg, job.title)
        limit = FORCE_MIN_AGE if job.force else self.max_age
        if fetched and self._now() - fetched < limit:
            job.cached = True
            if job.force:
                minutes = max(1, round((self._now() - fetched).total_seconds() / 60))
                job.notes.append(
                    f"Ropewiki was already checked {minutes} min ago; wait a few minutes to "
                    "check again."
                )
            return
        self._say(job, "Fetching the canyon from Ropewiki")
        scratch = self.work / f"{slug}.ingest.gpkg"
        scratch.unlink(missing_ok=True)
        try:
            self._ingest(job.title, scratch, self.root / "kml_cache")
            pgstore.load_geopackage(pg, scratch)
        except Exception as err:
            if fetched is None:
                raise BuildError(f"Could not fetch {job.title} from Ropewiki: {err}") from err
            job.notes.append(
                f"Ropewiki could not be reached ({err}); the canyon data is from "
                f"{fetched:%Y-%m-%d %H:%M} UTC."
            )
        finally:
            scratch.unlink(missing_ok=True)

    # ---- map tiles ----------------------------------------------------------------

    def _tile_base(self, job: Job, pg: Any, slug: str) -> Path | None:
        title = job.title
        with pg.cursor() as cur:
            cur.execute(
                "SELECT status, path, message, built_at FROM canyon.tile_builds"
                " WHERE ropewiki_title = %s",
                (title,),
            )
            row = cur.fetchone()
        if row:
            status, path, message, built = row
            if status == "ready" and path and Path(path).exists():
                job.tiles = "included"
                return self._refresh_tiles(job, pg, slug) or Path(path)
            retry = TILE_RETRY.get(status)
            built_at = pgstore._parse_time(built)
            if retry and built_at and self._now() - built_at < retry:
                job.tiles = status
                if message:
                    job.notes.append(message)
                return None
        return self._build_tiles(job, pg, slug)

    def _tile_state(self, pg: Any, title: str) -> tuple[str, str | None, dict, dict] | None:
        """(status, artifact path, layers, attempts) of a canyon's map. Layers an artifact made
        before they were recorded are read from the file and saved."""
        with pg.cursor() as cur:
            cur.execute(
                "SELECT status, path, layers, attempts FROM canyon.tile_builds"
                " WHERE ropewiki_title = %s",
                (title,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        status, path, layers, attempts = row
        layers, attempts = dict(layers or {}), dict(attempts or {})
        if status == "ready" and path and Path(path).exists() and not layers:
            layers = tile_stamps_of(Path(path))
            self._save_layers(pg, title, layers, attempts)
        return status, path, layers, attempts

    def _cropped_dem(self, pg: Any, title: str) -> Path | None:
        """The canyon's own cropped DEM, if one was kept."""
        with pg.cursor() as cur:
            cur.execute(
                "SELECT dem_path FROM canyon.tile_builds WHERE ropewiki_title = %s", (title,)
            )
            row = cur.fetchone()
        return Path(row[0]) if row and row[0] and Path(row[0]).exists() else None

    def _keep_dem(self, pg: Any, title: str, slug: str, cache: Path) -> None:
        """Keep the build's cropped DEM (a small, compressed GeoTIFF of just the mapped area) and
        the list of raw tiles it was cut from."""
        mosaic = cache / "dem_mosaic.tif"
        if not mosaic.exists():
            return
        kept = self.dems / f"{slug}.tif"
        os.replace(mosaic, kept)  # the cache is on the same filesystem as the kept copy
        try:
            sources = json.loads((cache / "dem_sources.json").read_text())
        except (OSError, ValueError):
            sources = []
        with pg.transaction(), pg.cursor() as cur:
            cur.execute(
                "UPDATE canyon.tile_builds SET dem_path = %s, dem_sources = %s::jsonb"
                " WHERE ropewiki_title = %s",
                (str(kept), json.dumps(sources), title),
            )

    def _save_layers(self, pg: Any, title: str, layers: dict, attempts: dict | None = None) -> None:
        with pg.transaction(), pg.cursor() as cur:
            cur.execute(
                "UPDATE canyon.tile_builds SET layers = %s::jsonb,"
                " attempts = COALESCE(%s::jsonb, attempts) WHERE ropewiki_title = %s",
                (json.dumps(layers), json.dumps(attempts) if attempts is not None else None, title),
            )

    def _refresh_tiles(self, job: Job, pg: Any, slug: str) -> Path | None:
        """Rebuild a ready map artifact when any of its layers is missing, built by older code
        or over a year old. The old artifact stays in use if the rebuild fails. Returns the
        artifact (None when there is none to refresh)."""
        state = self._tile_state(pg, job.title)
        if state is None or state[0] != "ready" or not state[1] or not Path(state[1]).exists():
            return None
        _, path, layers, attempts = state
        old = Path(path)
        todo = layers_to_rebuild(layers, attempts, self._now())
        if not todo:
            return old
        names = ", ".join(LAYER_LABELS.get(t, t) for t in todo)
        self._say(job, f"Rebuilding map layers ({names})")
        stamp = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        dem = self._cropped_dem(pg, job.title)
        if set(todo) == {"tiles_terrain"} and dem is not None:
            # Only the elevation layer, and the cropped DEM is kept: add it to a copy of the
            # artifact. No downloads, and the other layers are untouched.
            rebuilt = self._add_terrain(job, slug, old, dem)
        else:
            rebuilt = self._build_tiles(job, pg, slug)
        if rebuilt is None:  # keep what we had, and do not try again straight away
            self._record_tiles(pg, job.title, "ready", old, None)
            self._save_layers(pg, job.title, layers, {**attempts, **dict.fromkeys(todo, stamp)})
            job.tiles = "included"
            return old
        fresh = tile_stamps_of(rebuilt)
        # whatever it is, a rebuild was just tried: not again straight away
        self._save_layers(pg, job.title, fresh, {**attempts, **dict.fromkeys(todo, stamp)})
        return rebuilt

    def _add_terrain(self, job: Job, slug: str, artifact: Path, dem: Path) -> Path | None:
        scratch_dir = self.work / slug
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch = scratch_dir / "tiles-terrain.gpkg"
        shutil.copyfile(artifact, scratch)
        cmd = [
            sys.executable,
            "-m",
            "canyongis.geopackage.tile_build",
            str(scratch),
            "--dem",
            str(dem),
            "--terrain",
            "--cache-dir",
            str(scratch_dir / "terrain-cache"),
        ]
        last: deque[str] = deque(maxlen=4)

        def on_line(line: str) -> None:
            if line.strip():
                last.append(line.strip())
                job.detail = line.strip()[:200]

        code = self._runner(cmd, on_line, TILE_TIMEOUT_SECONDS)
        try:
            if code != 0 or not scratch.exists():
                job.notes.append(
                    "The 3D elevation layer could not be added: " + " | ".join(last)[:300]
                )
                return None
            os.replace(scratch, artifact)
            return artifact
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def _record_tiles(
        self, pg: Any, title: str, status: str, path: Path | None, message: str | None
    ):
        with pg.transaction(), pg.cursor() as cur:
            cur.execute(
                "INSERT INTO canyon.tile_builds (ropewiki_title, status, path, message, built_at)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ropewiki_title) DO UPDATE SET"
                " status = EXCLUDED.status, path = EXCLUDED.path, message = EXCLUDED.message,"
                " built_at = EXCLUDED.built_at",
                (
                    title,
                    status,
                    str(path) if path else None,
                    message,
                    self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )

    def _build_tiles(self, job: Job, pg: Any, slug: str) -> Path | None:
        title = job.title
        scratch_dir = self.work / slug
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch = scratch_dir / "tiles-build.gpkg"
        self._say(job, "Checking what map data exists for this area")
        try:
            pgstore.export_geopackage(pg, [title], scratch)
            plan = self._plan_tiles(scratch)
        except TileUnavailable as err:
            self._record_tiles(pg, title, "unavailable", None, str(err))
            job.tiles = "unavailable"
            job.notes.append(str(err))
            return None
        except Exception as err:
            message = f"Could not check map coverage ({err}); only the canyon data is included."
            self._record_tiles(pg, title, "failed", None, message)
            job.tiles = "failed"
            job.notes.append(message)
            return None
        cache = self.work / f"{slug}-cache"
        kept = self._cropped_dem(pg, title)
        cmd = [
            sys.executable,
            "-m",
            "canyongis.geopackage.tile_build",
            str(scratch),
            # a kept cropped DEM needs no download; otherwise the shared raw tiles are fetched
            # (once, whichever canyon needs them first)
            *(["--dem", str(kept)] if kept else ["--fetch-dem"]),
            "--topo",
            "--terrain",
            "--cache-dir",
            str(cache),
            "--dem-source-dir",
            str(self.dem_sources),
        ]
        if plan.get("naip_scenes"):
            cmd.insert(4, "--fetch-naip")
        gigabytes = plan.get("dem_bytes", 0) / 1e9
        self._say(
            job,
            f"Building the offline map ({plan['tiles']} tiles, "
            f"{gigabytes:.1f} GB of elevation data to download the first time)",
        )
        last: deque[str] = deque(maxlen=4)

        def on_line(line: str) -> None:
            if line.strip():
                last.append(line.strip())
                job.detail = line.strip()[:200]

        code = self._runner(cmd, on_line, TILE_TIMEOUT_SECONDS)
        if code != 0 or not scratch.exists():
            message = "The offline map could not be built: " + " | ".join(last)[:300]
            self._record_tiles(pg, title, "failed", None, message)
            job.tiles = "failed"
            job.notes.append(message)
            return None  # the downloads in `cache` are kept, so a retry resumes
        artifact = self.tile_files / f"{slug}.gpkg"
        os.replace(scratch, artifact)
        self._record_tiles(pg, title, "ready", artifact, None)
        self._keep_dem(pg, title, slug, cache)
        built = tile_stamps_of(artifact)
        stamp = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        # layers this area cannot have (no aerial imagery, say) are not hunted for again soon
        self._save_layers(
            pg, title, built, {t: stamp for t in tiles.LAYER_VERSIONS if t not in built}
        )
        shutil.rmtree(cache, ignore_errors=True)  # per-canyon scratch; the DEMs were kept above
        shutil.rmtree(scratch_dir, ignore_errors=True)
        job.tiles = "included"
        return artifact


# -----------------------------------------------------------------------------
# HTTP routing (the handler in serve.py only moves bytes)
# -----------------------------------------------------------------------------


def handle(
    service: BuildService,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    content_type: str = "",
) -> tuple[int, dict[str, Any]]:
    """Answer an ``/api/...`` request: (HTTP status, JSON-able body)."""
    try:
        if method == "GET" and path == "/api/status":
            return 200, {
                "ok": True,
                "tiles": service.tiles_enabled,
                "max_age_hours": service.max_age.total_seconds() / 3600,
            }
        if method == "GET" and path == "/api/search":
            text = (parse_qs(query).get("q") or [""])[0][:200]
            try:
                return 200, {"results": service.search(text)}
            except OSError as err:
                return 502, {"error": f"Ropewiki search failed: {err}"}
        if method == "POST" and path == "/api/builds":
            # JSON only: a cross-site form cannot send this content type without a preflight.
            if not content_type.lower().startswith("application/json"):
                return 415, {"error": "Send JSON (Content-Type: application/json)."}
            if len(body) > MAX_BODY_BYTES:
                return 413, {"error": "Request too large."}
            try:
                data = json.loads(body or b"{}")
                title = data["title"]
                if not isinstance(title, str):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                return 400, {"error": 'Send {"title": "<Ropewiki canyon title>"}.'}
            want_tiles = data.get("tiles", True) is not False
            try:
                job = service.request_build(title, want_tiles=want_tiles)
            except ValueError as err:
                return 400, {"error": str(err)}
            except OSError as err:
                return 502, {"error": f"Could not check the canyon on Ropewiki: {err}"}
            return 202, {"job": job.public()}
        if method == "GET" and path == "/api/manifest":
            title = (parse_qs(query).get("title") or [""])[0][:200]
            if not title or _TITLE_FORBIDDEN.search(title):
                return 400, {"error": "Give the canyon's Ropewiki title as ?title=."}
            info = service.manifest(title)
            if info is None:
                return 404, {"error": "This server does not have that canyon yet."}
            return 200, {"manifest": info}
        if method == "POST" and path == "/api/updates":
            if not content_type.lower().startswith("application/json"):
                return 415, {"error": "Send JSON (Content-Type: application/json)."}
            if len(body) > MAX_BODY_BYTES:
                return 413, {"error": "Request too large."}
            try:
                data = json.loads(body or b"{}")
                title, have = data["title"], data.get("have", {})
                have_tiles = data.get("tiles")
                if not isinstance(title, str) or not isinstance(have, dict):
                    raise TypeError
                if have_tiles is not None:
                    have_tiles = {
                        k: {"version": v.get("version"), "built_at": v.get("built_at")}
                        for k, v in have_tiles.items()
                        if isinstance(k, str) and isinstance(v, dict)
                    }
            except (ValueError, KeyError, TypeError):
                return 400, {"error": 'Send {"title": "...", "have": {"<section>": "<digest>"}}.'}
            try:
                job = service.request_update(
                    title, have, force=data.get("force") is True, have_tiles=have_tiles
                )
            except ValueError as err:
                return 400, {"error": str(err)}
            except OSError as err:
                return 502, {"error": f"Could not check the canyon on Ropewiki: {err}"}
            return 202, {"job": job.public()}
        match = re.fullmatch(r"/api/updates/([0-9a-f]{32})/delta", path)
        if method == "GET" and match:
            delta = service.delta(match.group(1))
            if delta is None:
                return 404, {"error": "No finished update with that id."}
            return 200, {"delta": delta}
        match = re.fullmatch(r"/api/builds/([0-9a-f]{32})", path)
        if method == "GET" and match:
            job = service.job(match.group(1))
            if job is None:
                return 404, {"error": "Unknown job (the server may have restarted)."}
            return 200, {"job": job.public()}
        if path.startswith("/api/"):
            known = ("/api/status", "/api/search", "/api/builds", "/api/manifest", "/api/updates")
            return (405 if path in known else 404), {"error": "Not found."}
    except Exception as err:
        log("api error:\n" + traceback.format_exc())
        return 500, {"error": f"Server error: {err}"}
    return 404, {"error": "Not found."}


def create_service(root: str | Path, *, tiles: bool = True) -> BuildService:
    """A service backed by the PostgreSQL named in CANYONGIS_PG_* (migrations applied)."""
    from ..web.db import apply_migrations, postgres_config_from_env

    config = postgres_config_from_env()
    apply_migrations(config)
    return BuildService(root, lambda: pgstore.connect(config), tiles=tiles)

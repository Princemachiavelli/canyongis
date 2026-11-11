from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from canyongis.geopackage import forecast as fc
from canyongis.geopackage import pgstore, viewer_api
from canyongis.geopackage.viewer_api import BuildService, TileUnavailable, handle

REPO = Path(__file__).resolve().parents[2]
LYTLE = REPO / "web" / "canyon-viewer" / "test" / "fixtures" / "lytle.gpkg"
TITLE = "Lytle Creek (Middle Fork)"
PAYLOAD = json.loads((REPO / "tests/fixtures/open-meteo.json").read_text())
PLAN = {"tiles": 1892, "dem_tiles": 11, "dem_bytes": 2_670_000_000, "naip_scenes": 4}


# ---- search -------------------------------------------------------------------------


def _smw(*entries: dict) -> dict:
    return {
        "query": {
            "results": [
                {
                    "fulltext": e["title"],
                    "printouts": {
                        "Has latitude": [e.get("lat")] if e.get("lat") is not None else [],
                        "Has longitude": [e.get("lon")] if e.get("lon") is not None else [],
                        "Has region": [{"fulltext": e["region"]}] if e.get("region") else [],
                        "Has technical rating": [e["tech"]] if e.get("tech") else [],
                        "Has water rating": [],
                        "Has number of rappels": [e["raps"]] if "raps" in e else [],
                    },
                }
                for e in entries
            ]
        }
    }


def test_query_words_keep_only_safe_characters() -> None:
    assert viewer_api.query_words("  Lytle   Creek ") == ["Lytle", "Creek"]
    assert viewer_api.query_words("a") == []
    assert viewer_api.query_words("]] [[ | ~ * ?") == []
    assert viewer_api.query_words("x]] [[Category:Secret|y") == ["x", "Category", "Secret", "y"]
    assert viewer_api.query_words("one two three four five six seven") == [
        "one",
        "two",
        "three",
        "four",
        "five",
    ]
    assert viewer_api.query_words("O'Brien's Rock-&-Roll") == ["O'Brien's", "Rock-&-Roll"]


def test_search_query_cannot_gain_conditions_from_hostile_text() -> None:
    plain = viewer_api.search_query(viewer_api.query_words("lytle creek"))
    hostile = viewer_api.search_query(viewer_api.query_words("lytle]] [[Category:X||~*|creek"))
    words = viewer_api.query_words("lytle]] [[Category:X||~*|creek")
    assert words == ["lytle", "Category", "X", "creek"]  # the brackets and pipes are gone
    assert hostile.count("[[") == 1 + len(words)  # one condition per word, nothing extra
    assert hostile.count("|?") == plain.count("|?")  # the same printouts
    for query in (plain, hostile):
        assert "[[Category:Canyons]]" in query
        # every condition is a bracket pair holding only wildcard alternatives
        conditions = query.split("|?")[0]
        for part in conditions.split(" "):
            assert part.startswith("[[") and part.endswith("]]"), part
        assert query.count("[[") == query.count("]]")
    assert viewer_api.case_variants("lytle") == ["lytle", "Lytle", "LYTLE"]


def test_search_parses_ranks_and_limits() -> None:
    asked = []

    def ask(query: str) -> dict:
        asked.append(query)
        return _smw(
            {
                "title": "Spring Canyon (Lytle Creek Area)",
                "region": "Lytle Creek Area",
                "tech": "3",
                "raps": 2,
                "lat": 34.2,
                "lon": -117.5,
            },
            {"title": "Lytle Creek (South Fork)"},
            {"title": "Lytle Creek (Middle Fork)", "raps": 5},
        )

    results = viewer_api.search_canyons("lytle creek", ask=ask, limit=2)
    assert [r["title"] for r in results] == [
        "Lytle Creek (Middle Fork)",
        "Lytle Creek (South Fork)",
    ]
    assert results[0]["rappels"] == 5 and results[0]["region"] is None
    full = viewer_api.search_canyons("spring lytle", ask=ask)
    assert full[-1]["region"] == "Lytle Creek Area" and full[-1]["technical_rating"] == "3"
    assert "~*Lytle*" in asked[0] and "~*creek*" in asked[0]
    assert viewer_api.search_canyons("", ask=ask) == [] and len(asked) == 2


def test_slugs_are_safe_file_names() -> None:
    for title in ("Lytle Creek (Middle Fork)", "../../etc/passwd", "Ünïcode Cañón", "///", ""):
        slug = viewer_api.slug_for(title)
        assert "/" not in slug and ".." not in slug and slug == slug.lower()
    assert viewer_api.slug_for("A") != viewer_api.slug_for("a")


# ---- the build service (needs PostGIS) ----------------------------------------------


class Rig:
    """A BuildService with Ropewiki, the tile build and the clock replaced by fakes."""

    def __init__(self, tmp_path: Path, canyon_db, **kwargs) -> None:
        import psycopg

        self.now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        self.ingests: list[str] = []
        self.plans = 0
        self.tile_runs: list[list[str]] = []
        self.plan_result: dict | Exception = PLAN
        self.runner_code = 0
        self.ingest_error: Exception | None = None
        self.mutate = None  # a function(sqlite connection): what Ropewiki changed
        self.service = BuildService(
            tmp_path / "svc",
            lambda: psycopg.connect(**canyon_db.connect_kwargs(), autocommit=True),
            ask=lambda q: _smw({"title": TITLE}),
            verify=lambda t: t != "Not a canyon",
            ingest=self.ingest,
            plan_tiles=self.plan,
            runner=self.runner,
            fetch_forecast=lambda lat, lon: fc.parse_forecast(PAYLOAD, "2026-11-09T12:00:00Z"),
            now=lambda: self.now,
            synchronous=kwargs.pop("synchronous", True),
            **kwargs,
        )

    def ingest(self, title: str, out: Path, cache: Path) -> None:
        self.ingests.append(title)
        if self.ingest_error:
            raise self.ingest_error
        shutil.copyfile(LYTLE, out)
        conn = sqlite3.connect(out)
        conn.execute(
            "UPDATE canyons SET ropewiki_title = ?, fetched_at = ?", (title, self.now.isoformat())
        )
        if self.mutate:
            self.mutate(conn)
        if title != TITLE:  # condition and vote pages belong to one canyon each
            conn.execute("UPDATE conditions SET source_title = source_title || ?", (title,))
            conn.execute("UPDATE ratings SET source_title = source_title || ?", (title,))
        conn.commit()
        conn.close()

    def plan(self, gpkg: Path) -> dict:
        self.plans += 1
        assert sqlite3.connect(gpkg).execute("SELECT count(*) FROM canyons").fetchone() == (1,)
        if isinstance(self.plan_result, Exception):
            raise self.plan_result
        return self.plan_result

    def runner(self, cmd: list[str], on_line, timeout: float) -> int:
        self.tile_runs.append(cmd)
        on_line("tiles_osm: wrote 674 tiles")
        if self.runner_code:
            on_line("boom: out of disk")
            return self.runner_code
        shutil.copyfile(LYTLE, cmd[3])  # the fixture is a GeoPackage with tile tables
        if "--fetch-dem" in cmd:  # a build that downloads leaves its cropped DEM in the cache
            cache = Path(cmd[cmd.index("--cache-dir") + 1])
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "dem_mosaic.tif").write_bytes(b"cropped dem")
            raw = Path(cmd[cmd.index("--dem-source-dir") + 1]) / "dem" / "tile1.tif"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"raw dem tile")
            (cache / "dem_sources.json").write_text(json.dumps([str(raw)]))
        return 0


@pytest.fixture
def rig(pg, canyon_db, tmp_path):
    return Rig(tmp_path, canyon_db)


def _tables(path: Path) -> set[str]:
    return {r[0] for r in sqlite3.connect(path).execute("SELECT table_name FROM gpkg_contents")}


def test_first_build_ingests_forecasts_tiles_and_exports(rig, pg) -> None:
    job = rig.service.request_build(TITLE)
    assert (job.state, job.error, job.tiles, job.cached) == ("done", None, "included", False)
    assert rig.ingests == [TITLE] and rig.plans == 1
    cmd = rig.tile_runs[0]
    assert cmd[1:3] == ["-m", "canyongis.geopackage.tile_build"]
    assert "--fetch-naip" in cmd and "--fetch-dem" in cmd and "--topo" in cmd
    out = rig.service.exports / job.file
    assert job.size == out.stat().st_size and job.public()["url"] == f"builds/{job.file}"
    tables = _tables(out)
    assert {"tiles_imagery", "tiles_hillshade", "canyons", "forecasts"} <= tables
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT fetched_at FROM forecasts").fetchone() == ("2026-11-09T12:00:00Z",)
    assert conn.execute("SELECT count(*) FROM forecast_days").fetchone() == (3,)
    assert pg.execute("SELECT status FROM canyon.tile_builds").fetchone() == ("ready",)
    assert not (rig.service.work / (viewer_api.slug_for(TITLE) + "-cache")).exists()


def test_second_request_reuses_data_and_tiles(rig) -> None:
    first = rig.service.request_build(TITLE)
    rig.now += timedelta(hours=2)
    second = rig.service.request_build(TITLE)
    assert second.id != first.id and second.state == "done" and second.cached
    assert rig.ingests == [TITLE] and rig.plans == 1 and len(rig.tile_runs) == 1
    assert second.tiles == "included"


def test_data_older_than_24_hours_is_refreshed_without_rebuilding_tiles(rig, pg) -> None:
    rig.service.request_build(TITLE)
    rig.now += timedelta(hours=25)
    job = rig.service.request_build(TITLE)
    assert job.state == "done" and not job.cached
    assert rig.ingests == [TITLE, TITLE] and len(rig.tile_runs) == 1
    assert job.tiles == "included"
    assert pg.execute("SELECT count(*) FROM canyon.canyons").fetchone()[0] == 1


def test_an_old_forecast_is_refreshed_at_export_time(rig, pg) -> None:
    rig.service.request_build(TITLE)
    pg.execute("UPDATE canyon.forecasts SET fetched_at = '2026-11-01T00:00:00Z'")
    pg.commit()  # or the service's own connection would wait for this row lock
    job = rig.service.request_build(TITLE)
    out = sqlite3.connect(rig.service.exports / job.file)
    assert out.execute("SELECT fetched_at FROM forecasts").fetchone() == ("2026-11-09T12:00:00Z",)


def test_no_map_data_gives_a_data_only_file_and_is_not_retried_for_a_week(rig, pg) -> None:
    rig.plan_result = TileUnavailable("No USGS 1 m elevation data covers this canyon.")
    job = rig.service.request_build(TITLE)
    assert job.state == "done" and job.tiles == "unavailable"
    assert job.notes == ["No USGS 1 m elevation data covers this canyon."]
    assert not any(t.startswith("tiles_") for t in _tables(rig.service.exports / job.file))
    assert rig.tile_runs == []
    again = rig.service.request_build(TITLE)
    assert rig.plans == 1 and again.tiles == "unavailable" and again.notes == job.notes
    rig.now += timedelta(days=8)
    rig.plan_result = PLAN
    assert rig.service.request_build(TITLE).tiles == "included" and rig.plans == 2


def test_a_failed_tile_build_still_delivers_the_data_and_is_retried_after_an_hour(rig) -> None:
    rig.runner_code = 1
    job = rig.service.request_build(TITLE)
    assert job.state == "done" and job.tiles == "failed"
    assert "boom: out of disk" in job.notes[0]
    assert not any(t.startswith("tiles_") for t in _tables(rig.service.exports / job.file))
    rig.now += timedelta(minutes=30)
    assert rig.service.request_build(TITLE).tiles == "failed" and len(rig.tile_runs) == 1
    rig.now += timedelta(hours=1)
    rig.runner_code = 0
    assert rig.service.request_build(TITLE).tiles == "included" and len(rig.tile_runs) == 2


def test_a_failed_coverage_check_is_a_failure_not_an_unavailable_area(rig) -> None:
    rig.plan_result = OSError("planetarycomputer unreachable")
    job = rig.service.request_build(TITLE)
    assert job.state == "done" and job.tiles == "failed" and "unreachable" in job.notes[0]


def test_data_only_request_skips_tiles(rig) -> None:
    job = rig.service.request_build(TITLE, want_tiles=False)
    assert job.state == "done" and job.tiles == "skipped" and rig.plans == 0
    assert not any(t.startswith("tiles_") for t in _tables(rig.service.exports / job.file))


def test_ropewiki_failure_fails_only_when_nothing_is_stored(rig) -> None:
    rig.ingest_error = OSError("ropewiki is down")
    job = rig.service.request_build(TITLE)
    assert job.state == "failed" and "ropewiki is down" in job.error
    assert rig.service._active == {}
    rig.ingest_error = None
    assert rig.service.request_build(TITLE).state == "done"
    rig.now += timedelta(days=2)
    rig.ingest_error = OSError("ropewiki is down")
    stale = rig.service.request_build(TITLE)
    assert stale.state == "done" and "from 2026-11-09 12:00 UTC" in stale.notes[0]


def test_a_stored_canyon_is_served_while_ropewiki_is_unreachable(rig) -> None:
    rig.service.request_build(TITLE)
    checked = []

    def verify(title: str) -> bool:
        checked.append(title)
        raise OSError("ropewiki is down")

    rig.service._verify = verify
    rig.ingest_error = OSError("ropewiki is down")
    rig.now += timedelta(days=2)
    job = rig.service.request_build(TITLE)
    assert checked == []  # stored canyons are not re-verified
    assert job.state == "done" and "from 2026-11-09 12:00 UTC" in job.notes[0]
    with pytest.raises(OSError):  # a canyon that is not stored still needs Ropewiki's word
        rig.service.request_build("Never Seen Before")
    assert (
        handle(
            rig.service,
            "POST",
            "/api/builds",
            "",
            b'{"title": "Never Seen Before"}',
            "application/json",
        )[0]
        == 502
    )


def test_unreadable_ropewiki_answers_become_bad_gateway(rig, monkeypatch) -> None:
    import canyongis.ingest as ingest

    def html_page(**params):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(ingest, "_ropewiki_query", html_page)
    with pytest.raises(OSError, match="unreadable"):
        viewer_api._default_ask("[[Category:Canyons]]")
    with pytest.raises(OSError, match="unreadable"):
        viewer_api._default_verify("Lytle Creek (Middle Fork)")
    rig.service._ask = viewer_api._default_ask
    assert handle(rig.service, "GET", "/api/search", "q=lytle")[0] == 502


def test_rejects_titles_that_are_not_canyons(rig) -> None:
    for bad in ("", "x" * 201, "Foo]] [[Bar", "a|b", "a{b}", "Not a canyon"):
        with pytest.raises(ValueError):
            rig.service.request_build(bad)
    assert rig.ingests == []


def test_search_marks_what_is_already_prepared(rig) -> None:
    assert rig.service.search("lytle")[0] | {} == {
        **rig.service.search("lytle")[0],
        "fresh": False,
        "map_ready": False,
    }
    rig.service.request_build(TITLE)
    hit = rig.service.search("lytle")[0]
    assert (hit["title"], hit["fresh"], hit["map_ready"]) == (TITLE, True, True)
    rig.now += timedelta(hours=30)
    assert rig.service.search("lytle")[0]["fresh"] is False


def test_concurrent_requests_join_the_running_job_and_builds_run_one_at_a_time(
    pg, canyon_db, tmp_path
) -> None:
    rig = Rig(tmp_path, canyon_db, synchronous=False)
    started, release = threading.Event(), threading.Event()
    real = rig.ingest

    def slow(title, out, cache):
        started.set()
        assert release.wait(20)
        real(title, out, cache)

    rig.service._ingest = slow
    first = rig.service.request_build(TITLE, want_tiles=False)
    assert started.wait(20)
    same = rig.service.request_build(TITLE, want_tiles=False)
    other = rig.service.request_build("Another Canyon", want_tiles=False)
    assert same is first and other is not first
    assert first.state == "running" and other.state == "queued"
    release.set()
    for job in (first, other):
        for _ in range(200):
            if job.state in ("done", "failed"):
                break
            threading.Event().wait(0.05)
    assert (first.state, other.state) == ("done", "done")
    assert rig.ingests.count(TITLE) == 1 and "Another Canyon" in rig.ingests


# ---- HTTP routing --------------------------------------------------------------------


def test_api_routes(rig) -> None:
    svc = rig.service
    assert handle(svc, "GET", "/api/status") == (
        200,
        {"ok": True, "tiles": True, "max_age_hours": 24.0},
    )
    status, body = handle(svc, "GET", "/api/search", "q=lytle")
    assert status == 200 and body["results"][0]["title"] == TITLE
    assert handle(svc, "GET", "/api/search", "")[1] == {"results": []}

    post = lambda payload, ctype="application/json": handle(  # noqa: E731
        svc,
        "POST",
        "/api/builds",
        "",
        payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
        ctype,
    )
    assert post({"title": TITLE}, "text/plain")[0] == 415
    assert post({"title": TITLE}, "application/x-www-form-urlencoded")[0] == 415
    assert post(b"not json")[0] == 400
    assert post({"nope": 1})[0] == 400
    assert post({"title": 5})[0] == 400
    assert post({"title": "Not a canyon"})[0] == 400
    assert post(b"{" + b" " * 5000 + b"}")[0] == 413
    status, body = post({"title": TITLE, "tiles": False})
    assert (
        status == 202
        and body["job"]["state"] == "done"
        and body["job"]["url"].startswith("builds/")
    )
    job_id = body["job"]["id"]
    assert handle(svc, "GET", f"/api/builds/{job_id}")[1]["job"]["id"] == job_id
    assert handle(svc, "GET", "/api/builds/" + "0" * 32)[0] == 404
    assert handle(svc, "GET", "/api/builds/../../etc")[0] == 404
    assert handle(svc, "DELETE", "/api/builds")[0] == 405
    assert handle(svc, "GET", "/api/other")[0] == 404
    assert handle(svc, "GET", "/elsewhere")[0] == 404


def test_search_failure_is_a_bad_gateway(rig) -> None:
    def broken(query: str) -> dict:
        raise OSError("ropewiki down")

    rig.service._ask = broken
    status, body = handle(rig.service, "GET", "/api/search", "q=lytle")
    assert status == 502 and "ropewiki down" in body["error"]


# ---- updates ---------------------------------------------------------------------------


def _digests(service, title: str = TITLE) -> dict[str, str]:
    return {n: v["digest"] for n, v in service.manifest(title)["sections"].items()}


def test_an_update_without_fingerprints_sends_every_section(rig) -> None:
    job = rig.service.request_update(TITLE, {})
    assert (job.kind, job.state, job.stage) == ("update", "done", "Update ready")
    assert [o["section"] for o in job.outdated] == list(pgstore.SECTIONS)
    assert job.outdated[0]["label"] == "Canyon details"
    delta = rig.service.delta(job.id)
    assert set(delta["sections"]) == set(pgstore.SECTIONS) and delta["title"] == TITLE
    assert rig.ingests == [TITLE]  # the canyon was new to the server


def test_an_update_with_current_fingerprints_sends_nothing(rig) -> None:
    rig.service.request_update(TITLE, {})
    job = rig.service.request_update(TITLE, _digests(rig.service))
    assert job.state == "done" and job.outdated == [] and job.stage == "Already up to date"
    assert rig.service.delta(job.id)["sections"] == {}
    assert rig.ingests == [TITLE]  # data under 24 hours old is not fetched again


def test_a_change_on_ropewiki_flags_only_the_section_it_touched(rig) -> None:
    rig.service.request_update(TITLE, {})
    have = _digests(rig.service)
    rig.now += timedelta(hours=25)
    rig.mutate = lambda conn: conn.execute("UPDATE rappels SET height_m = 77 WHERE sequence = 1")
    job = rig.service.request_update(TITLE, have)
    assert [o["section"] for o in job.outdated] == ["geometry"]
    assert rig.ingests == [TITLE, TITLE]
    delta = rig.service.delta(job.id)
    assert list(delta["sections"]) == ["geometry"]
    rappels = next(t for t in delta["sections"]["geometry"]["tables"] if t["name"] == "rappels")
    assert 77.0 in [r[rappels["columns"].index("height_m")] for r in rappels["rows"]]
    # asking again with what it now has: nothing left to do
    assert rig.service.request_update(TITLE, _digests(rig.service)).outdated == []


def test_an_update_still_works_when_ropewiki_is_unreachable(rig) -> None:
    rig.service.request_update(TITLE, {})
    rig.now += timedelta(days=2)
    rig.ingest_error = OSError("ropewiki is down")
    job = rig.service.request_update(TITLE, {"beta": "old"})
    assert job.state == "done" and "from 2026-11-09 12:00 UTC" in job.notes[0]
    assert "beta" in [o["section"] for o in job.outdated]


def test_update_requests_are_checked(rig) -> None:
    with pytest.raises(ValueError):
        rig.service.request_update("Foo]] [[Bar", {})
    with pytest.raises(ValueError):
        rig.service.request_update("Not a canyon", {})
    job = rig.service.request_update(TITLE, {"bogus": "x", "beta": 5, "links": "y"})
    assert job.have == {"links": "y"}  # unknown sections and non-text values are dropped


def test_update_api_routes(rig) -> None:
    svc = rig.service
    assert handle(svc, "GET", "/api/manifest", "title=" + "x" * 300)[0] in (400, 404)
    assert handle(svc, "GET", "/api/manifest", "")[0] == 400
    assert handle(svc, "GET", "/api/manifest", "title=Foo%5D%5D")[0] == 400
    assert handle(svc, "GET", "/api/manifest", "title=Lytle")[0] == 404
    post = lambda payload, ctype="application/json": handle(  # noqa: E731
        svc,
        "POST",
        "/api/updates",
        "",
        payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
        ctype,
    )
    assert post({"title": TITLE}, "text/plain")[0] == 415
    assert post(b"nope")[0] == 400
    assert post({"have": {}})[0] == 400
    assert post({"title": TITLE, "have": ["x"]})[0] == 400
    assert post({"title": "Not a canyon"})[0] == 400
    assert post(b"{" + b" " * 5000 + b"}")[0] == 413
    status, body = post({"title": TITLE, "have": {}})
    assert status == 202 and body["job"]["kind"] == "update" and len(body["job"]["outdated"]) == 6
    job_id = body["job"]["id"]
    assert handle(svc, "GET", f"/api/builds/{job_id}")[1]["job"]["state"] == "done"
    status, body = handle(svc, "GET", f"/api/updates/{job_id}/delta")
    assert status == 200 and len(body["delta"]["sections"]) == 6
    status, body = handle(svc, "GET", "/api/manifest", "title=" + TITLE.replace(" ", "%20"))
    assert status == 200 and set(body["manifest"]["sections"]) == set(pgstore.SECTIONS)
    assert handle(svc, "GET", "/api/updates/" + "0" * 32 + "/delta")[0] == 404
    built = svc.request_build(TITLE, want_tiles=False)  # a build job has no delta
    assert handle(svc, "GET", f"/api/updates/{built.id}/delta")[0] == 404
    assert handle(svc, "GET", "/api/updates")[0] == 405


def test_forcing_an_update_skips_the_24_hour_rule_but_not_the_five_minute_one(rig) -> None:
    rig.service.request_update(TITLE, {})
    rig.now += timedelta(minutes=2)
    soon = rig.service.request_update(TITLE, {}, force=True)
    assert rig.ingests == [TITLE] and "checked 2 min ago" in soon.notes[0]
    rig.now += timedelta(minutes=10)
    forced = rig.service.request_update(TITLE, {}, force=True)
    assert rig.ingests == [TITLE, TITLE] and forced.notes == []  # well inside 24 hours
    plain = rig.service.request_update(TITLE, {})
    assert rig.ingests == [TITLE, TITLE] and plain.cached  # not forced: the 24 hour rule holds
    status, _ = handle(
        rig.service,
        "POST",
        "/api/updates",
        "",
        json.dumps({"title": TITLE, "have": {}, "force": True}).encode(),
        "application/json",
    )
    assert status == 202


# ---- tile layer tracking: versions and age ------------------------------------------


def _layers(pg) -> dict:
    return pg.execute("SELECT layers FROM canyon.tile_builds").fetchone()[0]


def test_layer_staleness_rules() -> None:
    from canyongis.geopackage import tiles

    now = datetime(2026, 11, 9, tzinfo=UTC)
    fresh = "2026-06-01T00:00:00Z"
    assert (
        tiles.layer_staleness(tiles.layer_version("tiles_terrain"), fresh, "tiles_terrain", now)
        is None
    )
    assert tiles.layer_staleness(None, fresh, "tiles_terrain", now) == "version"
    assert tiles.layer_staleness(0, fresh, "tiles_terrain", now) == "version"
    assert tiles.layer_staleness(1, "2025-11-08T00:00:00Z", "tiles_terrain", now) == "age"
    assert tiles.layer_staleness(1, "2025-11-10T00:00:00Z", "tiles_terrain", now) is None
    assert tiles.layer_staleness(1, "not a date", "tiles_terrain", now) == "unknown"
    assert tiles.layer_staleness(1, None, "tiles_terrain", now) == "unknown"


def test_a_built_layer_is_stamped_with_its_version_and_time(tmp_path) -> None:
    from canyongis.geopackage import tiles

    conn = sqlite3.connect(LYTLE)
    legacy = tiles.read_tile_stamps(conn)  # no stamps yet: version 1, built at last_change
    assert legacy["tiles_imagery"] == {"version": 1, "built_at": "2026-11-09T05:34:37.951Z"}
    copy = tmp_path / "c.gpkg"
    shutil.copyfile(LYTLE, copy)
    conn = sqlite3.connect(copy)
    tiles.stamp_tile_layer(conn, "tiles_imagery")
    held = tiles.read_tile_stamps(conn)["tiles_imagery"]
    assert held["version"] == tiles.layer_version("tiles_imagery")
    assert held["built_at"] > legacy["tiles_imagery"]["built_at"]


def test_a_build_records_each_layer_and_what_the_area_cannot_have(rig, pg) -> None:
    rig.service.request_build(TITLE)
    layers = _layers(pg)
    assert set(layers) == {"tiles_imagery", "tiles_hillshade"}
    assert layers["tiles_imagery"]["version"] == 1
    attempts = pg.execute("SELECT attempts FROM canyon.tile_builds").fetchone()[0]
    assert {"tiles_terrain", "tiles_osm"} <= set(
        attempts
    )  # looked for, not there: not retried soon
    info = rig.service.manifest(TITLE)
    assert info["map"]["layers"]["tiles_imagery"]["stale"] is None
    assert info["map"]["layers"]["tiles_imagery"]["current_version"] == 1


def test_update_reports_layers_the_device_lacks_or_holds_older_builds_of(rig) -> None:
    rig.service.request_build(TITLE)
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert {t["layer"]: t["reason"] for t in job.tiles_outdated} == {
        "tiles_imagery": "missing",
        "tiles_hillshade": "missing",
    }
    assert job.tiles_outdated[0]["label"] and job.public()["tiles_outdated"]
    assert job.stage == "Update ready" and len(rig.tile_runs) == 1  # nothing was rebuilt
    mine = {  # a device with an older imagery build and a current hillshade
        "tiles_imagery": {"version": 1, "built_at": "2026-01-01T00:00:00Z"},
        "tiles_hillshade": {"version": 1, "built_at": "2026-11-09T05:34:37.982Z"},
    }
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles=mine)
    assert [(t["layer"], t["reason"]) for t in job.tiles_outdated] == [("tiles_imagery", "newer")]
    mine["tiles_imagery"]["built_at"] = "2026-11-09T05:34:37.951Z"
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles=mine)
    assert job.tiles_outdated == [] and job.stage == "Already up to date"
    # a client that does not send its layers is not told about them
    assert rig.service.request_update(TITLE, _digests(rig.service)).tiles_outdated == []


def test_layers_over_a_year_old_are_rebuilt_on_the_next_update(rig, pg) -> None:
    rig.service.request_build(TITLE)
    rig.now += timedelta(days=366)
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert job.state == "done" and len(rig.tile_runs) == 2  # rebuilt: older than a year
    assert "--terrain" in rig.tile_runs[1]
    # the fixture stands in for the build, so its stamps are old again: not retried for a day
    rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert len(rig.tile_runs) == 2
    rig.now += timedelta(days=2)
    rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert len(rig.tile_runs) == 3


def test_a_failed_rebuild_keeps_the_old_map(rig, pg) -> None:
    rig.service.request_build(TITLE)
    rig.now += timedelta(days=400)
    rig.runner_code = 1
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert job.state == "done" and len(rig.tile_runs) == 2
    status, path = pg.execute("SELECT status, path FROM canyon.tile_builds").fetchone()
    assert status == "ready" and Path(path).exists()
    assert set(_layers(pg)) == {"tiles_imagery", "tiles_hillshade"}
    assert {t["layer"] for t in job.tiles_outdated} == {"tiles_imagery", "tiles_hillshade"}


def test_the_updates_endpoint_takes_the_layers_a_device_holds(rig) -> None:
    rig.service.request_build(TITLE)
    body = json.dumps({"title": TITLE, "have": {}, "tiles": {"tiles_imagery": {"version": 1}}})
    status, out = handle(
        rig.service, "POST", "/api/updates", body=body.encode(), content_type="application/json"
    )
    assert status == 202
    job = rig.service.job(out["job"]["id"])
    assert job.have_tiles == {"tiles_imagery": {"version": 1, "built_at": None}}


def test_manifest_says_which_layers_the_next_update_will_rebuild(rig) -> None:
    rig.service.request_build(TITLE)
    info = rig.service.manifest(TITLE)
    assert info["map"]["pending"] == {}  # what the area lacks was looked for at the build
    rig.now += timedelta(days=366)
    info = rig.service.manifest(TITLE)
    assert (
        info["map"]["pending"]
        == {
            "tiles_imagery": "age",
            "tiles_hillshade": "age",
            "tiles_osm": "missing",
            "tiles_contours_m": "missing",
            "tiles_contours_ft": "missing",
            "tiles_terrain": "missing",
        }
        and info["map"]["layers"]["tiles_imagery"]["stale"] == "age"
    )


def test_a_map_built_before_layers_were_tracked_is_flagged_and_gets_terrain(rig, pg) -> None:
    """The state of a server that already had cached canyons when tracking was added."""
    rig.service.request_build(TITLE)
    pg.execute("UPDATE canyon.tile_builds SET layers = '{}', attempts = '{}'")
    pg.commit()
    info = rig.service.manifest(TITLE)  # reads the stamps back from the artifact file
    assert "tiles_terrain" in info["map"]["pending"] and set(info["map"]["layers"]) == {
        "tiles_imagery",
        "tiles_hillshade",
    }
    # even a device that sends no layers makes the server rebuild what it lacks
    job = rig.service.request_update(TITLE, _digests(rig.service))
    assert job.state == "done" and len(rig.tile_runs) == 2 and "--terrain" in rig.tile_runs[1]
    assert "tiles_terrain" in pg.execute("SELECT attempts FROM canyon.tile_builds").fetchone()[0]
    assert rig.service.manifest(TITLE)["map"]["pending"] == {}
    # and a device that does send them is told what it lacks
    job = rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    assert {t["layer"] for t in job.tiles_outdated} == {"tiles_imagery", "tiles_hillshade"}


def test_a_build_keeps_its_cropped_dem_and_the_raw_tiles_it_came_from(rig, pg) -> None:
    rig.service.request_build(TITLE)
    cmd = rig.tile_runs[0]
    assert cmd[cmd.index("--dem-source-dir") + 1] == str(rig.service.dem_sources)
    kept = rig.service.dems / f"{viewer_api.slug_for(TITLE)}.tif"
    assert kept.read_bytes() == b"cropped dem"
    path, sources = pg.execute("SELECT dem_path, dem_sources FROM canyon.tile_builds").fetchone()
    assert path == str(kept)
    assert len(sources) == 1 and Path(sources[0]).read_bytes() == b"raw dem tile"
    # the per-canyon scratch is gone; the raw tiles are shared and stay for a later cleanup
    assert not (rig.service.work / (viewer_api.slug_for(TITLE) + "-cache")).exists()
    assert Path(sources[0]).parent.parent == rig.service.dem_sources


def test_a_rebuild_uses_the_kept_dem_instead_of_downloading(rig) -> None:
    rig.service.request_build(TITLE)
    rig.now += timedelta(days=366)  # every layer is over a year old
    rig.service.request_update(TITLE, _digests(rig.service), have_tiles={})
    cmd = rig.tile_runs[1]
    kept = rig.service.dems / f"{viewer_api.slug_for(TITLE)}.tif"
    assert cmd[cmd.index("--dem") + 1] == str(kept) and "--fetch-dem" not in cmd
    assert "--topo" in cmd and "--terrain" in cmd


def test_a_missing_terrain_layer_alone_is_added_from_the_kept_dem(rig, pg) -> None:
    rig.service.request_build(TITLE)
    fresh = {"version": 1, "built_at": "2026-11-09T12:00:00Z"}
    layers = {t: fresh for t in viewer_api.tiles.LAYER_VERSIONS if t != "tiles_terrain"}
    pg.execute(
        "UPDATE canyon.tile_builds SET layers = %s::jsonb, attempts = '{}'", (json.dumps(layers),)
    )
    pg.commit()
    job = rig.service.request_update(TITLE, _digests(rig.service))
    assert job.state == "done" and len(rig.tile_runs) == 2
    cmd = rig.tile_runs[1]
    kept = rig.service.dems / f"{viewer_api.slug_for(TITLE)}.tif"
    assert cmd[cmd.index("--dem") + 1] == str(kept)
    assert "--terrain" in cmd and "--topo" not in cmd and "--fetch-dem" not in cmd
    assert "--fetch-naip" not in cmd  # nothing is downloaded
    status, path = pg.execute("SELECT status, path FROM canyon.tile_builds").fetchone()
    assert status == "ready" and Path(path).exists()
    assert not (rig.service.work / viewer_api.slug_for(TITLE)).exists()


def test_without_a_kept_dem_missing_terrain_means_a_full_rebuild(rig, pg) -> None:
    rig.service.request_build(TITLE)
    fresh = {"version": 1, "built_at": "2026-11-09T12:00:00Z"}
    layers = {t: fresh for t in viewer_api.tiles.LAYER_VERSIONS if t != "tiles_terrain"}
    pg.execute(
        "UPDATE canyon.tile_builds SET layers = %s::jsonb, attempts = '{}', dem_path = NULL",
        (json.dumps(layers),),
    )
    pg.commit()
    rig.service.request_update(TITLE, _digests(rig.service))
    assert len(rig.tile_runs) == 2 and "--fetch-dem" in rig.tile_runs[1]

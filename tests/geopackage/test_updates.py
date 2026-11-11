"""Section fingerprints, the manifest and deltas: how a client learns what is out of date."""

from __future__ import annotations

import base64
import re
import sqlite3
from pathlib import Path

import pytest

from canyongis.geopackage import pgstore

REPO = Path(__file__).resolve().parents[2]
LYTLE = REPO / "web" / "canyon-viewer" / "test" / "fixtures" / "lytle.gpkg"
TITLE = "Lytle Creek (Middle Fork)"


@pytest.fixture
def loaded(pg):
    pgstore.load_geopackage(pg, LYTLE)
    return pg


def _stamps(pg) -> dict[str, tuple[str, str]]:
    rows = pg.execute("SELECT section, digest, updated_at FROM canyon.section_stamps").fetchall()
    return {s: (d, u) for s, d, u in rows}


def _canyon_id(pg) -> int:
    return pg.execute("SELECT id FROM canyon.canyons").fetchone()[0]


def test_loading_a_canyon_stamps_every_section(loaded) -> None:
    stamps = _stamps(loaded)
    assert set(stamps) == set(pgstore.SECTIONS)
    for digest, updated in stamps.values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", updated)
    assert len({d for d, _ in stamps.values()}) == len(stamps)  # sections differ from each other


def test_reloading_unchanged_data_changes_nothing(loaded, monkeypatch) -> None:
    before = _stamps(loaded)
    monkeypatch.setattr(pgstore, "_now_iso", lambda: "2099-01-01T00:00:00Z")
    pgstore.load_geopackage(loaded, LYTLE)  # re-creates every child row with new ids
    assert _stamps(loaded) == before


def test_fetch_times_are_not_content(loaded, monkeypatch) -> None:
    before = _stamps(loaded)
    monkeypatch.setattr(pgstore, "_now_iso", lambda: "2099-01-01T00:00:00Z")
    loaded.execute("UPDATE canyon.canyons SET fetched_at = '2099-01-01T00:00:00+00:00'")
    loaded.execute("UPDATE canyon.kml_files SET downloaded_at = '2099-01-01T00:00:00+00:00'")
    pgstore.refresh_stamps(loaded, _canyon_id(loaded))
    assert _stamps(loaded) == before


def test_only_the_section_that_changed_gets_a_new_stamp(loaded, monkeypatch) -> None:
    before = _stamps(loaded)
    monkeypatch.setattr(pgstore, "_now_iso", lambda: "2099-01-01T00:00:00Z")
    loaded.execute("UPDATE canyon.beta SET body_text = 'rewritten' WHERE position = 0")
    pgstore.refresh_stamps(loaded, _canyon_id(loaded))
    after = _stamps(loaded)
    assert {s for s in after if after[s] != before[s]} == {"beta"}
    assert after["beta"][1] == "2099-01-01T00:00:00Z"


def test_each_kind_of_change_is_seen_in_its_own_section(loaded, monkeypatch) -> None:
    cid = _canyon_id(loaded)
    changes = {
        "canyon": "UPDATE canyon.canyons SET region = 'Elsewhere'",
        "geometry": "UPDATE canyon.rappels SET height_m = 99 WHERE sequence = 1",
        "links": f"INSERT INTO canyon.links (canyon_id, url) VALUES ({cid}, 'http://new.example')",
        "conditions": "UPDATE canyon.condition_rappel_notes SET note = 'changed'",
        "ratings": "UPDATE canyon.ratings SET rating = 1",
    }
    for section, statement in changes.items():
        before = _stamps(loaded)
        loaded.execute(statement)
        pgstore.refresh_stamps(loaded, cid)
        after = _stamps(loaded)
        assert {s for s in after if after[s][0] != before[s][0]} == {section}, section


def test_a_note_moved_to_another_report_changes_the_conditions_fingerprint(loaded) -> None:
    cid = _canyon_id(loaded)
    before = _stamps(loaded)["conditions"][0]
    loaded.execute(
        "UPDATE canyon.condition_rappel_notes SET report_id = "
        "(SELECT id FROM canyon.conditions WHERE id <> report_id LIMIT 1)"
        " WHERE id = (SELECT min(id) FROM canyon.condition_rappel_notes)"
    )
    pgstore.refresh_stamps(loaded, cid)
    assert _stamps(loaded)["conditions"][0] != before


def test_manifest_lists_sections_with_labels(loaded) -> None:
    info = pgstore.manifest(loaded, TITLE)
    assert info["title"] == TITLE and info["schema_version"] == "5" and info["map"] is None
    assert list(info["sections"]) == list(pgstore.SECTIONS)
    assert info["sections"]["conditions"]["label"] == "Condition reports"
    assert info["sections"]["beta"]["digest"] == _stamps(loaded)["beta"][0]
    assert info["data_fetched_at"]
    assert pgstore.manifest(loaded, "No such canyon") is None
    loaded.execute(
        "INSERT INTO canyon.tile_builds VALUES (%s, 'ready', '/x', NULL, '2026-11-09T00:00:00Z')",
        (TITLE,),
    )
    assert pgstore.manifest(loaded, TITLE)["map"] == {
        "status": "ready",
        "built_at": "2026-11-09T00:00:00Z",
        "layers": {},
    }


def test_a_canyon_stored_before_stamps_existed_is_stamped_on_demand(loaded) -> None:
    loaded.execute("DELETE FROM canyon.section_stamps")
    info = pgstore.manifest(loaded, TITLE)
    assert set(info["sections"]) == set(pgstore.SECTIONS) and len(_stamps(loaded)) == 6


def test_outdated_sections_compares_digests(loaded) -> None:
    info = pgstore.manifest(loaded, TITLE)
    have = {name: s["digest"] for name, s in info["sections"].items()}
    assert pgstore.outdated_sections(info, have) == []
    have["beta"] = "stale"
    del have["links"]
    assert pgstore.outdated_sections(info, have) == ["beta", "links"]
    assert pgstore.outdated_sections(info, {}) == list(pgstore.SECTIONS)


def test_a_delta_carries_the_rows_of_the_sections_asked_for(loaded) -> None:
    delta = pgstore.section_delta(loaded, TITLE, ["geometry", "conditions"])
    assert delta["title"] == TITLE and set(delta["sections"]) == {"geometry", "conditions"}
    geometry = delta["sections"]["geometry"]
    assert [t["name"] for t in geometry["tables"]] == [
        "kml_files",
        "routes",
        "waypoints",
        "rappels",
    ]
    routes = next(t for t in geometry["tables"] if t["name"] == "routes")
    assert routes["mode"] == "replace" and routes["geometry"] == "geom"
    assert routes["fks"] == {"canyon_id": "canyons", "kml_file_id": "kml_files"}
    assert routes["columns"][0] == "id" and "color" in routes["columns"]
    blob = base64.b64decode(routes["rows"][0][routes["columns"].index("geom")])
    assert blob[:2] == b"GP" and len(blob) > 29
    rappels = next(t for t in geometry["tables"] if t["name"] == "rappels")
    assert len(rappels["rows"]) == 5
    conditions = delta["sections"]["conditions"]["tables"]
    assert [t["name"] for t in conditions] == [
        "conditions",
        "condition_rappel_notes",
        "condition_photos",
    ]
    assert conditions[1]["fks"] == {"report_id": "conditions"}
    assert len(conditions[0]["rows"]) == 2
    # the fingerprint and time ride along, matching the manifest
    info = pgstore.manifest(loaded, TITLE)
    assert geometry["digest"] == info["sections"]["geometry"]["digest"]
    assert geometry["updated_at"] == info["sections"]["geometry"]["updated_at"]


def test_the_canyon_row_is_updated_in_place(loaded) -> None:
    table = pgstore.section_delta(loaded, TITLE, ["canyon"])["sections"]["canyon"]["tables"][0]
    assert table["name"] == "canyons" and table["mode"] == "update" and len(table["rows"]) == 1
    assert table["rows"][0][table["columns"].index("ropewiki_title")] == TITLE


def test_a_delta_rejects_unknown_sections_and_canyons(loaded) -> None:
    with pytest.raises(ValueError):
        pgstore.section_delta(loaded, TITLE, ["forecast"])
    with pytest.raises(KeyError):
        pgstore.section_delta(loaded, "No such canyon", ["beta"])


def test_an_export_carries_the_fingerprints_and_loading_it_back_ignores_them(
    loaded, tmp_path
) -> None:
    out = tmp_path / "export.gpkg"
    pgstore.export_geopackage(loaded, [TITLE], out)
    conn = sqlite3.connect(out)
    rows = conn.execute(
        "SELECT s.section, s.digest, s.updated_at FROM section_stamps s"
        " JOIN canyons c ON c.id = s.canyon_id WHERE c.ropewiki_title = ?",
        (TITLE,),
    ).fetchall()
    assert {s: (d, u) for s, d, u in rows} == _stamps(loaded)
    before = _stamps(loaded)
    assert pgstore.load_geopackage(loaded, out) == [TITLE]  # no clash with its own stamps
    assert _stamps(loaded) == before

"""Ropewiki page authors and licence: fetched, stored, exported, updated and never lost."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from canyongis.geopackage import pgstore
from canyongis.geopackage import ropewiki_convert as rc
from canyongis.geopackage.ropewiki_convert import CanyonRow, _replace_canyon
from canyongis.geopackage.schema import create_geopackage, upgrade_geopackage

REPO = Path(__file__).resolve().parents[2]
LYTLE = REPO / "web" / "canyon-viewer" / "test" / "fixtures" / "lytle.gpkg"
TITLE = "Lytle Creek (Middle Fork)"
ATTRIBUTION = {
    "authors": ["Dangel", "Bjp", "A. Anderson"],
    "anonymous": 2,
    "license_name": "Creative Commons Attribution Non-Commercial Share Alike",
    "license_url": "http://creativecommons.org/licenses/by-nc-sa/3.0/",
}


def test_fetch_attribution_lists_every_contributor_across_pages(monkeypatch) -> None:
    pages = [
        {
            "query": {
                "pages": [
                    {"contributors": [{"userid": 1, "name": "Dangel"}], "anoncontributors": 3}
                ]
            },
            "continue": {"pccontinue": "2"},
        },
        {
            "query": {
                "pages": [
                    {
                        "contributors": [
                            {"userid": 2, "name": "Bjp"},
                            {"userid": 1, "name": "Dangel"},
                        ]
                    }
                ]
            }
        },
    ]
    seen = []

    def fake_query(**params):
        seen.append(params)
        if params.get("meta") == "siteinfo":
            return {"query": {"rightsinfo": {"text": "CC BY-NC-SA", "url": "http://cc/by-nc-sa"}}}
        return pages.pop(0)

    monkeypatch.setattr(rc, "_query", fake_query)
    monkeypatch.setattr(rc, "_RIGHTS", None)
    got = rc.fetch_attribution("KC Canyon")
    assert got == {
        "authors": ["Dangel", "Bjp"],
        "anonymous": 3,
        "license_name": "CC BY-NC-SA",
        "license_url": "http://cc/by-nc-sa",
    }
    assert seen[1]["pccontinue"] == "2" and seen[0]["pclimit"] == "max"
    rc.fetch_attribution("KC Canyon") if pages else None
    assert sum(1 for p in seen if p.get("meta") == "siteinfo") == 1  # the licence is fetched once


def _canyon_row(attribution) -> CanyonRow:
    return CanyonRow(TITLE, 1, 1, {}, [], [], [], attribution=attribution)


def test_the_converter_stores_authors_and_licence(tmp_path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _replace_canyon(conn, _canyon_row(ATTRIBUTION), "u", None)
    assert conn.execute("SELECT name FROM authors ORDER BY id").fetchall() == [
        ("Dangel",),
        ("Bjp",),
        ("A. Anderson",),
    ]
    assert conn.execute(
        "SELECT license_name, license_url, anonymous_authors FROM canyons"
    ).fetchone() == (ATTRIBUTION["license_name"], ATTRIBUTION["license_url"], 2)
    _replace_canyon(conn, _canyon_row(None), "u", None)  # nothing fetched: no authors, no licence
    assert conn.execute("SELECT count(*) FROM authors").fetchone() == (0,)


def test_older_files_gain_the_attribution_table_and_columns(tmp_path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    conn.execute("DROP TABLE authors")
    conn.execute("DELETE FROM gpkg_contents WHERE table_name = 'authors'")
    for column in ("license_name", "license_url", "anonymous_authors"):
        conn.execute(f"ALTER TABLE canyons DROP COLUMN {column}")
    upgrade_geopackage(conn)
    upgrade_geopackage(conn)
    assert conn.execute("SELECT count(*) FROM authors").fetchone() == (0,)
    assert "license_url" in {r[1] for r in conn.execute("PRAGMA table_info(canyons)")}


@pytest.fixture
def with_authors(pg, tmp_path):
    """The Lytle fixture, with attribution, loaded into PostGIS."""
    path = tmp_path / "attributed.gpkg"
    import shutil

    shutil.copyfile(LYTLE, path)
    conn = sqlite3.connect(path)
    upgrade_geopackage(conn)
    conn.execute(
        "UPDATE canyons SET license_name = ?, license_url = ?, anonymous_authors = 2",
        (ATTRIBUTION["license_name"], ATTRIBUTION["license_url"]),
    )
    conn.executemany(
        "INSERT INTO authors (canyon_id, name) VALUES (1, ?)",
        [(n,) for n in ATTRIBUTION["authors"]],
    )
    conn.commit()
    conn.close()
    pgstore.load_geopackage(pg, path)
    return pg, path


def test_attribution_round_trips_through_postgis_and_into_exports(with_authors, tmp_path) -> None:
    pg, _ = with_authors
    out = tmp_path / "out.gpkg"
    pgstore.export_geopackage(pg, [TITLE], out)
    back = sqlite3.connect(out)
    assert [r[0] for r in back.execute("SELECT name FROM authors ORDER BY name")] == sorted(
        ATTRIBUTION["authors"]
    )
    assert back.execute("SELECT license_url, anonymous_authors FROM canyons").fetchone() == (
        ATTRIBUTION["license_url"],
        2,
    )


def test_a_reingest_without_attribution_keeps_it(with_authors, tmp_path) -> None:
    pg, _ = with_authors
    bare = tmp_path / "bare.gpkg"
    import shutil

    shutil.copyfile(LYTLE, bare)  # as if the authors could not be fetched this time
    pgstore.load_geopackage(pg, bare)
    assert pg.execute("SELECT count(*) FROM canyon.authors").fetchone()[0] == 3
    assert (
        pg.execute("SELECT license_name FROM canyon.canyons").fetchone()[0].startswith("Creative")
    )


def test_a_reingest_with_a_new_author_list_replaces_it(with_authors, tmp_path) -> None:
    pg, path = with_authors
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM authors")
    conn.executemany(
        "INSERT INTO authors (canyon_id, name) VALUES (1, ?)", [("Newcomer",), ("Bjp",)]
    )
    conn.commit()
    conn.close()
    before = pgstore.manifest(pg, TITLE)["sections"]["canyon"]["digest"]
    pgstore.load_geopackage(pg, path)
    names = sorted(r[0] for r in pg.execute("SELECT name FROM canyon.authors").fetchall())
    assert names == ["Bjp", "Newcomer"]
    assert pgstore.manifest(pg, TITLE)["sections"]["canyon"]["digest"] != before


def test_authors_travel_in_the_canyon_section_of_a_delta(with_authors) -> None:
    pg, _ = with_authors
    tables = pgstore.section_delta(pg, TITLE, ["canyon"])["sections"]["canyon"]["tables"]
    assert [t["name"] for t in tables] == ["canyons", "authors"]
    assert tables[1]["mode"] == "replace" and len(tables[1]["rows"]) == 3
    assert "license_url" in tables[0]["columns"]

"""What the KML itself says survives conversion: line colours, node names and descriptions.

tests/fixtures/ropewiki/KC_Canyon.kml is the KML Ropewiki serves for KC Canyon: three lines
(Exit yellow, Approach green, Descent red) and four named nodes with descriptions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from canyongis.geopackage.ropewiki_convert import CanyonRow, _insert_geometry, parse_kml_geometry
from canyongis.geopackage.schema import create_geopackage, upgrade_geopackage
from canyongis.geopackage.wikitext import Rappel
from canyongis.validate import kml_color_to_hex, parse_kml, plain_text

KC = Path(__file__).resolve().parents[1] / "fixtures" / "ropewiki" / "KC_Canyon.kml"


def test_kml_colours_are_aabbggrr() -> None:
    assert kml_color_to_hex("FF0000FF") == "#ff0000"  # red
    assert kml_color_to_hex("ff00ff00") == "#00ff00"  # green
    assert kml_color_to_hex("FF00FFFF") == "#ffff00"  # yellow
    assert kml_color_to_hex("80FF8040") == "#4080ff"  # alpha dropped
    assert kml_color_to_hex("#0000ff") == "#ff0000"  # no alpha: bbggrr
    for bad in (None, "", "red", "12345", "gg0000ff"):
        assert kml_color_to_hex(bad) is None


def test_plain_text_keeps_breaks_and_drops_markup() -> None:
    assert plain_text("R1-R3 are back to back.") == "R1-R3 are back to back."
    assert plain_text("<p>One &amp; two</p><br/>Three<br>  four ") == "One & two\nThree\nfour"
    assert plain_text("  \n ") is None and plain_text(None) is None


def test_kc_canyon_lines_keep_their_own_colours() -> None:
    feats = parse_kml(KC)
    lines = {
        r["name"]: r["color"]
        for frame in (feats.descent_lines, feats.approach_lines, feats.unclassified_lines)
        for _, r in frame.iterrows()
    }
    assert lines == {"Exit": "#ffff00", "Approach": "#00ff00", "Descent": "#ff0000"}


def test_kc_canyon_nodes_keep_name_and_description() -> None:
    feats = parse_kml(KC)
    nodes = {
        r["name"]: (None if pd.isna(r["description"]) else r["description"])
        for _, r in feats.points.iterrows()
    }
    assert nodes == {
        "R1-R5": "R1-R3 are back to back, then short walk to R4 and R5.",
        "R7": "Or might be R6?",
        "R8": "105'",
        "R10-R12 Sequence": None,
    }


def test_shared_styles_and_style_maps_are_followed(tmp_path: Path) -> None:
    kml = tmp_path / "shared.kml"
    kml.write_text(
        """<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>
        <Style id="blue"><LineStyle><color>ffff0000</color></LineStyle></Style>
        <StyleMap id="map"><Pair><key>normal</key><styleUrl>#blue</styleUrl></Pair>
          <Pair><key>highlight</key><styleUrl>#other</styleUrl></Pair></StyleMap>
        <Placemark><name>Descent A</name><styleUrl>#blue</styleUrl><LineString>
          <coordinates>-111,40,0 -111,40.001,0</coordinates></LineString></Placemark>
        <Placemark><name>Descent B</name><styleUrl>#map</styleUrl><LineString>
          <coordinates>-111,40,0 -111,40.002,0</coordinates></LineString></Placemark>
        <Placemark><name>Descent C</name><LineString>
          <coordinates>-111,40,0 -111,40.003,0</coordinates></LineString></Placemark>
        <Placemark><name>R1</name><description><![CDATA[<b>Big</b> drop<br>keep left]]></description>
          <Point><coordinates>-111,40.0005,0</coordinates></Point></Placemark>
        </Document></kml>"""
    )
    feats = parse_kml(kml)
    colours = {
        r["name"]: (None if pd.isna(r["color"]) else r["color"])
        for _, r in feats.descent_lines.iterrows()
    }
    assert colours == {"Descent A": "#0000ff", "Descent B": "#0000ff", "Descent C": None}
    assert feats.points.iloc[0]["description"] == "Big drop\nkeep left"


def _store(conn: sqlite3.Connection, rappels: list[Rappel]) -> None:
    conn.execute(
        "INSERT INTO canyons (ropewiki_title, raw_template_json, source_url, fetched_at)"
        " VALUES ('KC Canyon', '{}', 'u', 'now')"
    )
    row = CanyonRow("KC Canyon", 1, 1, {}, [], rappels, [])
    _insert_geometry(conn, 1, row, parse_kml_geometry(KC), {})
    conn.commit()


def test_kml_only_nodes_become_rappels_that_keep_the_kml_name_and_description(
    tmp_path: Path,
) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _store(conn, [])
    rows = conn.execute(
        "SELECT sequence, label, kml_name, kml_description FROM rappels ORDER BY sequence"
    ).fetchall()
    assert rows == [
        (1, "R1", "R1-R5", "R1-R3 are back to back, then short walk to R4 and R5."),
        (2, "R7", "R7", "Or might be R6?"),
        (3, "R8", "R8", "105'"),
        (4, "R10", "R10-R12 Sequence", None),
    ]
    colours = dict(conn.execute("SELECT name, color FROM routes"))
    assert colours == {"Descent": "#ff0000", "Exit": "#ffff00", "Approach": "#00ff00"}
    assert conn.execute("SELECT count(*) FROM waypoints").fetchone() == (0,)


def test_a_wiki_rappel_matched_to_a_node_gets_the_nodes_name_and_description(
    tmp_path: Path,
) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    _store(conn, [Rappel("R1", 24.4, "80 ft"), Rappel("R2", None, "")])
    first = conn.execute(
        "SELECT label, height_m, kml_name, kml_description, geom IS NOT NULL FROM rappels"
        " WHERE sequence = 1"
    ).fetchone()
    assert first == (
        "R1",
        24.4,
        "R1-R5",
        "R1-R3 are back to back, then short walk to R4 and R5.",
        1,
    )
    second = conn.execute("SELECT kml_name, geom IS NOT NULL FROM rappels WHERE sequence = 2")
    assert second.fetchone() == (None, 0)  # R2 has no node of its own


def test_older_files_gain_the_new_columns(tmp_path: Path) -> None:
    conn = create_geopackage(tmp_path / "t.gpkg")
    for table, column in (
        ("routes", "color"),
        ("waypoints", "description"),
        ("rappels", "kml_name"),
        ("rappels", "kml_description"),
    ):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    upgrade_geopackage(conn)
    upgrade_geopackage(conn)  # idempotent
    for table, column in (
        ("routes", "color"),
        ("waypoints", "description"),
        ("rappels", "kml_name"),
        ("rappels", "kml_description"),
    ):
        assert column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone() == ("5",)

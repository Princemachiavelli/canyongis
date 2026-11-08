from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from canyongis.geopackage.geometry import blob_envelope, linestring_blob, point_blob
from canyongis.geopackage.ropewiki_convert import (
    _replace_canyon,
    build_row,
    condition_record,
    rating_record,
)
from canyongis.geopackage.schema import (
    APPLICATION_ID,
    FEATURE_TABLES,
    create_geopackage,
    open_geopackage,
    refresh_extents,
)
from canyongis.geopackage.wikitext import (
    external_links,
    find_template,
    height_m,
    parse_coded,
    parse_comments,
    parse_file_list,
    rappels,
    split_sections,
    strip_template,
    wiki_to_html,
    wiki_to_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ropewiki"
LYTLE = (FIXTURES / "Lytle_Creek_(Middle_Fork).wiki").read_text(encoding="utf-8")
CONDITION_TITLE = "Conditions:Lytle Creek (Middle Fork)-20260921035610"
CONDITION = {
    "pageid": 1,
    "revid": 99,
    "timestamp": "2026-09-21T04:38:03Z",
    "text": (FIXTURES / "Conditions_Lytle_Creek_(Middle_Fork)-20260921035610.wiki").read_text(
        encoding="utf-8"
    ),
}


def _page(text: str = LYTLE) -> dict:
    return {"pageid": 1234, "revid": 5678, "timestamp": "2026-01-01T00:00:00Z", "text": text}


def test_point_blob_header_and_envelope() -> None:
    blob = point_blob(-117.57694, 34.2533)
    assert blob[:2] == b"GP"
    assert struct.unpack_from("<i", blob, 4)[0] == 4326
    assert blob_envelope(blob) == (-117.57694, 34.2533, -117.57694, 34.2533)


def test_linestring_blob_envelope() -> None:
    blob = linestring_blob([(1.0, 2.0), (3.0, -4.0), (2.5, 5.0)])
    assert blob_envelope(blob) == (1.0, -4.0, 3.0, 5.0)


def test_linestring_needs_two_points() -> None:
    with pytest.raises(ValueError):
        linestring_blob([(0.0, 0.0)])


def test_template_parameters_and_sections() -> None:
    tpl = find_template(LYTLE, "Canyon")
    assert tpl is not None
    assert tpl["Coordinates"] == "34.2533, -117.57694"
    assert tpl["Number of rappels"] == "5"
    assert tpl["AKA"].startswith("Lytle Creek Canyon")
    headings = [h for h, _ in split_sections(strip_template(LYTLE, "Canyon"))]
    assert headings == ["Introduction", "Approach", "Descent", "Exit", "Red tape",
                        "Beta sites", "Trip reports and media", "Background"]


def test_rappel_lines_and_heights() -> None:
    found = rappels(LYTLE)
    assert [r.label for r in found] == ["R1", "R2", "R3", "R4", "R5"]
    assert found[0].height_m == pytest.approx(10.7, abs=0.1)  # ~35ft
    assert found[2].height_m == pytest.approx(25.9, abs=0.1)  # 85ft


def test_height_units() -> None:
    assert height_m("~35ft from two bolts") == pytest.approx(10.7, abs=0.1)
    assert height_m("12 m drop") == 12.0
    assert height_m("no number here") is None


def test_links_from_beta_sites_section() -> None:
    beta = dict(split_sections(LYTLE))["Beta sites"]
    urls = [url for url, _ in external_links(beta)]
    assert "http://brennen.caltech.edu/advents/midlyt.htm" in urls
    assert all(url.startswith("http") for url in urls)


def test_condition_report_structure() -> None:
    cond = condition_record(CONDITION_TITLE, CONDITION)
    assert cond is not None
    assert cond["trip_date"] == "2026-09-20"
    assert cond["reported_at"] == "2026-09-21T03:56:10"
    assert cond["revised_at"] == "2026-09-21T04:38:03Z"
    assert cond["quality"] == (2, "Ok")
    assert cond["water"] == (1, "Very Low", "class B = a2")
    assert cond["wetsuit"] == (2, "Thin wetsuit", "3mm-4mm")
    assert cond["difficulty"] == (2, "Normal", None)
    assert cond["team_size"] == 3
    assert cond["team_time_h"] == 5.5
    assert cond["photos"] == ["20260920 122828.jpg", "20260920 124421.jpg"]
    assert "Road:" in cond["general_notes"]
    assert "R1:" not in cond["general_notes"]


def test_condition_rappel_notes_split_multi_labels() -> None:
    cond = condition_record(CONDITION_TITLE, CONDITION)
    labels = [label for label, _ in cond["rappel_notes"]]
    assert labels == ["R1", "R2", "R3", "R4", "R5"]
    notes = dict(cond["rappel_notes"])
    assert notes["R1"] == "Bolt is slightly loose"
    assert notes["R5"] == "Webbing is good"


def test_parse_coded_and_comments_helpers() -> None:
    assert parse_coded("1 - Very Low (class B = a2)") == (1, "Very Low", "class B = a2")
    assert parse_coded("") == (None, None, None)
    assert parse_coded("Unknown") == (None, "Unknown", None)
    assert parse_file_list("a.jpg, b.jpg,") == ["a.jpg", "b.jpg"]
    general, notes = parse_comments("Intro line.\n\nR1: one\ncontinued\n\nR2 & R3: two")
    assert general == "Intro line."
    assert notes == [(["R1"], "one\ncontinued"), (["R2", "R3"], "two")]


def test_wiki_to_text_strips_markup() -> None:
    wiki = (
        "See [[Lytle Creek|the creek]] and [[Brennen]] at [http://x.org Brennen's page]."
        "{{Canyon|x=1}}<!-- hidden --> '''Bold''' ''it''&nbsp;done."
        "<ref>cite</ref>\n* item\n[[File:img.jpg|link=]]"
    )
    assert wiki_to_text(wiki) == "See the creek and Brennen at Brennen's page. Bold it done.\n- item"


def test_rappel_text_is_cleaned_in_sections() -> None:
    text = wiki_to_text(dict(split_sections(LYTLE))["Descent"])
    assert "'''" not in text
    assert "[[" not in text and "{{" not in text
    assert text.startswith("After suiting up")


def test_rating_record() -> None:
    rating = rating_record("Votes:Demo/Ryan.k", {"text": "{{Page rating\n|Rating=4\n|User=Ryan.k\n}}"})
    assert rating == {"source_title": "Votes:Demo/Ryan.k", "rater": "Ryan.k", "rating": 4}


def test_create_geopackage_registers_schema(tmp_path: Path) -> None:
    path = tmp_path / "canyons.gpkg"
    conn = create_geopackage(path)
    conn.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    srs = conn.execute("SELECT srs_id FROM gpkg_spatial_ref_sys ORDER BY srs_id").fetchall()
    assert srs == [(-1,), (0,), (4326,)]
    features = dict(conn.execute("SELECT table_name, geometry_type_name FROM gpkg_geometry_columns"))
    assert features == {name: geom for name, _, _, geom in FEATURE_TABLES}
    conn.close()
    with pytest.raises(FileExistsError):
        create_geopackage(path)


def test_write_canyon_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "canyons.gpkg"
    conn = create_geopackage(path)
    row = build_row("Lytle Creek (Middle Fork)", _page(), find_template(LYTLE, "Canyon"))
    row.conditions = [condition_record(CONDITION_TITLE, CONDITION)]
    row.ratings = [rating_record("Votes:Demo/X", {"text": "{{Page rating\n|Rating=3\n|User=X\n}}"})]
    for _ in range(2):
        _replace_canyon(conn, row, "https://ropewiki.com/Lytle_Creek_(Middle_Fork)", None)
        conn.commit()
    refresh_extents(conn)

    def count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert count("canyons") == 1
    assert count("rappels") == 5  # one per wiki rappel line; geometry stays NULL without KML
    assert count("beta") == 8
    assert count("conditions") == 1
    assert count("condition_rappel_notes") == 5
    assert count("condition_photos") == 2
    assert count("ratings") == 1
    assert count("links") > 0

    canyon = conn.execute(
        "SELECT technical_rating, number_of_rappels, longest_rappel_m, geom FROM canyons"
    ).fetchone()
    assert canyon[0] == 3
    assert canyon[1] == 5
    assert canyon[2] == pytest.approx(30.48, abs=0.01)  # 100 ft
    assert blob_envelope(canyon[3]) == (-117.57694, 34.2533, -117.57694, 34.2533)

    extents = conn.execute(
        "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name='canyons'"
    ).fetchone()
    assert extents == (-117.57694, 34.2533, -117.57694, 34.2533)
    conn.close()


def test_conditions_ordered_by_trip_date(tmp_path: Path) -> None:
    path = tmp_path / "canyons.gpkg"
    conn = create_geopackage(path)
    row = build_row("Lytle Creek (Middle Fork)", _page(), find_template(LYTLE, "Canyon"))
    later = condition_record(CONDITION_TITLE, CONDITION)
    earlier = dict(later, source_title="Conditions:Lytle Creek (Middle Fork)-20140615182115",
                   trip_date="2014-06-14")
    row.conditions = [later, earlier]
    _replace_canyon(conn, row, "https://ropewiki.com/x", None)
    conn.commit()
    dates = [r[0] for r in conn.execute("SELECT trip_date FROM conditions ORDER BY trip_date")]
    assert dates == ["2014-06-14", "2026-09-20"]
    conn.close()


def test_foreign_keys_cascade_on_delete(tmp_path: Path) -> None:
    path = tmp_path / "canyons.gpkg"
    conn = create_geopackage(path)
    row = build_row("Lytle Creek (Middle Fork)", _page(), find_template(LYTLE, "Canyon"))
    row.conditions = [condition_record(CONDITION_TITLE, CONDITION)]
    _replace_canyon(conn, row, "https://ropewiki.com/x", None)
    conn.commit()
    conn.execute("DELETE FROM canyons")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM beta").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM condition_photos").fetchone()[0] == 0
    conn.close()


def test_open_existing_geopackage(tmp_path: Path) -> None:
    path = tmp_path / "canyons.gpkg"
    create_geopackage(path).close()
    conn = open_geopackage(path)
    assert conn.execute("SELECT COUNT(*) FROM canyons").fetchone()[0] == 0
    conn.close()


def test_missing_trip_date_falls_back_to_filing_date() -> None:
    page = {"text": "{{Condition\n|ReportedBy=X\n|Team size=1\n}}", "revid": 1, "timestamp": None}
    cond = condition_record("Conditions:Demo-20200714021458", page)
    assert cond["trip_date"] == "2020-07-14"
    assert cond["trip_date_source"] == "filed_date"


def test_wiki_to_html_links_lists_and_escaping() -> None:
    html_out = wiki_to_html(
        "Go to [[Lytle Creek]] or [[Brennen|the hikes]], see http://x.org/a.\n"
        "* one ''it'' '''bold'''\n* two\n\n<script>a</script> a & b"
    )
    assert '<a href="https://ropewiki.com/Lytle_Creek">Lytle Creek</a>' in html_out
    assert '<a href="https://ropewiki.com/Brennen">the hikes</a>' in html_out
    assert '<a href="http://x.org/a">http://x.org/a</a>.' in html_out
    assert "<ul><li>one <i>it</i> <b>bold</b></li><li>two</li></ul>" in html_out
    assert "a &amp; b" in html_out
    assert "<script" not in html_out


def test_wiki_to_html_only_emits_allowed_tags() -> None:
    import re

    text = wiki_to_html(dict(split_sections(LYTLE))["Beta sites"] + "\n\n" + LYTLE[:400])
    tags = set(re.findall(r"</?([a-z0-9]+)", text))
    assert tags <= {"p", "a", "b", "i", "br", "ul", "ol", "li", "h4"}

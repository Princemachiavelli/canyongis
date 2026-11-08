"""Build test/fixtures/lytle.gpkg from the Lytle Creek wiki fixtures.

Run from the repo root inside `nix develop` (needs canyongis imports):
    python web/canyon-viewer/test/make_fixture.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from canyongis.geopackage.geometry import linestring_blob, point_blob
from canyongis.geopackage.ropewiki_convert import (
    _replace_canyon,
    build_row,
    condition_record,
    rating_record,
)
from canyongis.geopackage.schema import create_geopackage, refresh_extents
from canyongis.geopackage.wikitext import find_template

REPO = Path(__file__).resolve().parents[3]
WIKI = REPO / "tests" / "fixtures" / "ropewiki"
OUT = Path(__file__).resolve().parent / "fixtures" / "lytle.gpkg"


def main() -> None:
    lytle = (WIKI / "Lytle_Creek_(Middle_Fork).wiki").read_text(encoding="utf-8")
    cond_text = (WIKI / "Conditions_Lytle_Creek_(Middle_Fork)-20260921035610.wiki").read_text(
        encoding="utf-8"
    )
    cond_title = "Conditions:Lytle Creek (Middle Fork)-20260921035610"
    page = {"pageid": 408, "revid": 318052, "timestamp": "2026-09-21T04:38:03Z", "text": lytle}

    if OUT.exists():
        OUT.unlink()
    conn = create_geopackage(OUT)
    row = build_row("Lytle Creek (Middle Fork)", page, find_template(lytle, "Canyon"))
    row.conditions = [
        condition_record(cond_title, {"text": cond_text, "revid": 1, "timestamp": None}),
        condition_record(
            "Conditions:Lytle Creek (Middle Fork)-20140615182115",
            {"text": "{{Condition\n|Date=2014/06/14\n|Quality condition=4 - Great\n"
                     "|Comments=Flowing well.\n}}", "revid": 2, "timestamp": None},
        ),
    ]
    row.ratings = [rating_record("Votes:Lytle Creek (Middle Fork)/Demo",
                                 {"text": "{{Page rating\n|Rating=4\n|User=Demo\n}}"})]
    _replace_canyon(conn, row, "https://ropewiki.com/Lytle_Creek_(Middle_Fork)", None)
    canyon_id = conn.execute("SELECT id FROM canyons").fetchone()[0]
    conn.execute(
        "INSERT INTO routes (canyon_id, kind, name, length_m, geom) VALUES (?,?,?,?,?)",
        (canyon_id, "descent", "Descent", 900.0,
         linestring_blob([(-117.5769, 34.2488), (-117.5763, 34.2490), (-117.5750, 34.2500)])),
    )
    conn.execute("UPDATE rappels SET geom = ? WHERE label = 'R1'", (point_blob(-117.5768, 34.2487),))
    conn.commit()
    refresh_extents(conn)
    conn.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

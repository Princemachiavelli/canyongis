"""Build test/fixtures/delta.json: a delta from the real server code, for the viewer's tests.

Run from the repo root inside `nix develop` (needs a PostgreSQL with PostGIS, like the dev
shell's). It loads the Lytle fixture into a throwaway database, changes some things the way a
Ropewiki edit would, and writes the delta for every section plus the manifest:

    python web/canyon-viewer/test/make_delta_fixture.py

The changes the viewer tests look for: region 'Changed Region', rappel R1 height 99 m, a new
link, an extra author, the first rating set to 1 star, and one condition report removed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from canyongis.geopackage import pgstore
from canyongis.web.db import PostgresConfig, apply_migrations, postgres_config_from_env

HERE = Path(__file__).resolve().parent
TITLE = "Lytle Creek (Middle Fork)"


def main() -> None:
    admin = postgres_config_from_env()
    name = f"canyongis_delta_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(**admin.connect_kwargs(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    cfg = PostgresConfig(
        host=admin.host, port=admin.port, dbname=name, user=admin.user, password=admin.password
    )
    try:
        apply_migrations(cfg)
        with pgstore.connect(cfg) as pg:
            pgstore.load_geopackage(pg, HERE / "fixtures" / "lytle.gpkg")
            before = pgstore.manifest(pg, TITLE)
            cid = pg.execute("SELECT id FROM canyon.canyons").fetchone()[0]
            pg.execute("UPDATE canyon.canyons SET region = 'Changed Region'")
            pg.execute("UPDATE canyon.rappels SET height_m = 99 WHERE sequence = 1")
            pg.execute(
                "INSERT INTO canyon.links (canyon_id, label, url, source_name)"
                " VALUES (%s, 'New beta', 'https://example.org/new', 'example.org')",
                (cid,),
            )
            pg.execute("INSERT INTO canyon.authors (canyon_id, name) VALUES (%s, 'Dangel')", (cid,))
            pg.execute(
                "UPDATE canyon.canyons SET license_name = 'Creative Commons Attribution "
                "Non-Commercial Share Alike', license_url = "
                "'http://creativecommons.org/licenses/by-nc-sa/3.0/', anonymous_authors = 2"
            )
            pg.execute(
                "UPDATE canyon.ratings SET rating = 1 WHERE id = "
                "(SELECT min(id) FROM canyon.ratings)"
            )
            pg.execute(
                "DELETE FROM canyon.conditions WHERE id = (SELECT max(id) FROM canyon.conditions)"
            )
            pgstore.refresh_stamps(pg, cid)
            delta = pgstore.section_delta(pg, TITLE, list(pgstore.SECTIONS))
            after = pgstore.manifest(pg, TITLE)
    finally:
        with psycopg.connect(**admin.connect_kwargs(), autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    out = {"before": before, "manifest": after, "delta": delta}
    (HERE / "fixtures" / "delta.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print("wrote", HERE / "fixtures" / "delta.json")


if __name__ == "__main__":
    main()

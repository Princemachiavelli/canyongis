# Canyon GeoPackage schema (v5)

Portable copy of canyon data. The canonical store is the `canyon` schema in PostGIS (see
"PostGIS store" below), which has the same table and column names, so a GeoPackage is an export
of it and loading one is a row-for-row copy.

Code: `src/canyongis/geopackage/` (`schema.py` holds the DDL, `ropewiki_convert.py` the
converter). Build a file with:

```sh
canyongis-ropewiki-gpkg runs/canyons.gpkg "Lytle Creek (Middle Fork)" \
    "https://ropewiki.com/Willow_Creek_Canyon" --cache-dir runs/ropewiki-gpkg/cache
```

Add `--no-kml` to skip KML downloads (no routes, waypoints or rappel geometry). Each stored
canyon also gets a cached weather forecast; add `--no-forecast` to skip it, or refresh later with
`canyongis-gpkg-forecast runs/canyons.gpkg` (older files are upgraded to v3 first).

Version 3 added `runs`, `run_events`, `run_fixes`, `forecasts` and `forecast_days` to the v2
tables. `upgrade_geopackage()` (Python) and `gpkg-write.js` (the viewer) add them to older files.

## Tables

| Table | Type | Geometry | One row per | Source |
|---|---|---|---|---|
| `canyons` | feature | POINT (infobox `Coordinates`) | canyon page | `{{Canyon}}` |
| `routes` | feature | LINESTRING | KML track | page KML |
| `waypoints` | feature | POINT | KML point that is not a rappel | page KML |
| `rappels` | feature | POINT (NULL if no KML anchor) | rappel | wiki rappel lines, KML anchors |
| `beta` | attribute | - | page section | page prose, as wiki and cleaned text |
| `links` | attribute | - | external link | "Beta sites" section |
| `conditions` | attribute | - | condition report | `Conditions:<canyon>-<timestamp>` |
| `condition_rappel_notes` | attribute | - | rappel note in a report | `R1:`, `R4 & R5:` lines in Comments |
| `condition_photos` | attribute | - | photo in a report | `FileList` |
| `ratings` | attribute | - | vote | `Votes:<canyon>/<user>` |
| `kml_files` | attribute | - | KML file | page KML (sha256, URL, download time) |
| `authors` | attribute | - | Ropewiki page author | `prop=contributors` of the canyon page |
| `section_stamps` | attribute | - | data section of a canyon | content fingerprints, made by the server |
| `forecasts` | attribute | - | canyon (one current forecast) | Open-Meteo, fetched at build time or by the viewer |
| `forecast_days` | attribute | - | forecast day | Open-Meteo daily values |
| `forecast_hours` | attribute | - | forecast hour | Open-Meteo hourly values |
| `runs` | feature | LINESTRING (from the fixes, NULL under 2) | recorded descent | canyon companion |
| `run_events` | feature | POINT (NULL without GPS) | event in a run | canyon companion |
| `run_fixes` | feature | POINT | GPS fix | canyon companion |
| `schema_meta` | attribute | - | key | schema version, generator |

GeoPackage bookkeeping tables (`gpkg_spatial_ref_sys`, `gpkg_contents`,
`gpkg_geometry_columns`) are present, and the file sets `application_id` and `user_version`
as GeoPackage 1.4 requires.

Relationships: every child row has `canyon_id` (foreign key, `ON DELETE CASCADE`).
`routes`, `waypoints` and `rappels` also reference the `kml_files` row they came from.
The run tables are the exception, see below. `ON DELETE CASCADE` only runs when a connection
sets `PRAGMA foreign_keys = ON`; sql.js (the viewer) does not, so the viewer deletes children
itself.

## PostGIS store

Alembic migration `0006_canyon_schema` creates the `canyon` schema with the tables above
(except the run tables, which live on devices), in the database named by `CANYONGIS_PG_*` (the
dev shell's server on port 5433). Differences from the file: geometry columns are PostGIS
`geometry(..., 4326)` with GiST indexes, ids are identity columns, and `tile_builds` is
bookkeeping for the map tile files (`status` ready / failed / unavailable, the file's `path`, a
`message`). The migration runs `CREATE EXTENSION postgis`, so it needs a role that may do that
(or the extension created beforehand).

`canyongis.geopackage.pgstore` copies in both directions, table by table:

- `load_geopackage(pg, file)`: a canyon is upserted by Ropewiki title, so its id is stable; its
  child rows are replaced; its forecast is replaced only by a newer one.
- `export_geopackage(pg, titles, out, base=None)`: writes a new file. With `base` (a GeoPackage
  that already holds map tiles) the vector tables are rewritten on a copy of it, so the tiles
  come along without being rebuilt.
- `refresh_forecast(pg, title)`: fetches a new forecast when the stored one is over 3 hours
  old; a failed request keeps the old one.

Ids are remapped on the way (foreign keys follow), and a test checks that the two schemas keep
the same columns and that GeoPackage to PostGIS and back changes no row. The viewer's download
API (`canyongis.geopackage.viewer_api`) is built on this: Ropewiki data is ingested when a canyon
is new or its data is over 24 hours old, the forecast is refreshed at export time, and every
download is a fresh export, so no old file is kept to go stale. Map tiles are the exception: they
are large and rarely change, so each canyon's tiles are built once into a file under the
server's build directory and reused by every export. Without US coverage (NAIP imagery and USGS
1 m DEM) there are no tiles and the export is data only.

## Weather forecast

`forecasts` holds one row per canyon (`canyon_id` unique): `source` (`open-meteo`), `fetched_at`
(UTC, whole seconds, so it compares as text), the `latitude` / `longitude` asked for, the model's
`elevation_m` and the local `timezone`. `forecast_days` has one row per local `date` (seven days):
`weather_code` (WMO), `temp_max_c`, `temp_min_c`, `precip_mm`, `precip_prob_pct`,
`wind_max_kmh`, `gust_max_kmh`, and local `sunrise` / `sunset` (`YYYY-MM-DDTHH:MM`).
`forecast_hours` has one row per local hour (`time`, `YYYY-MM-DDTHH:MM`, 168 rows for seven days):
`weather_code`, `temp_c`, `precip_mm`, `precip_prob_pct`, `wind_kmh` and `gust_kmh`. A file or
forecast made before hourly values existed has none.

It is a cache: the converter and `canyongis-gpkg-forecast` replace a canyon's forecast, keep the
old one when the network is down, and the viewer shows `fetched_at` as "last updated". The viewer
fetches newer forecasts itself and writes the newest into a saved GeoPackage; a forecast never
replaces a newer one. Open-Meteo's free endpoint is for non-commercial use.

## Runs recorded by the companion

The canyon companion app stores runs on the device and writes them into a GeoPackage on request.

- `runs`: `run_uuid` (unique, made by the app), `canyon_title`, `started_at`, `ended_at`,
  `next_index` (the rappel the run was at), `source`, and `geom`, the GPS track.
- `run_events`: `run_uuid`, `seq` (time order within the run), `t`, `type` (`start`, `next`,
  `back`, `jump`, `mark`, `finish`), `rappel_sequence` (matches `rappels.sequence`), `acc` (m) and
  `geom`. A `mark` event is the user saving their position as the real anchor location.
- `run_fixes`: `run_uuid`, `seq`, `t`, `acc` (m), `geom`.

Runs are keyed by `canyon_title` and `rappel_sequence`, not by `canyons.id`, and have no foreign
key to `canyons`: re-running the converter replaces a canyon row, and a cascade would delete the
runs recorded for it. Times are ISO 8601 UTC with milliseconds. Saving is idempotent: a run's
events and fixes are replaced as a whole, runs already in the file that the saving device does
not have (for example from another phone) are kept, and runs for canyons not in the file are not
written. Deleting a run on the device does not remove it from a file that already contains it.

## Attribution

Ropewiki's text and data are licensed CC BY-NC-SA, so each canyon carries its credit:

- `authors`: one row per registered contributor Ropewiki lists for the canyon's page (bots
  included, as Ropewiki lists them), in the order the API returns them.
- `canyons.anonymous_authors`: how many anonymous contributors there are.
- `canyons.license_name` / `license_url`: the site's licence (from `siteinfo`).
- `canyons.source_url`: the page itself.

The viewer shows these at the bottom of a canyon's Overview. They are part of the `canyon` section
below, so they update with the rest. If Ropewiki cannot be reached when a canyon is ingested, the
canyon is stored without them; the server keeps any it already has rather than replacing them
with nothing.

## Updates

A downloaded canyon can be brought up to date without downloading the file again. The server
splits a canyon's data into sections that change independently (`canyon` = the infobox row and
authors, `geometry` = KML tracks, waypoints and rappels, `beta` = description, `links`,
`conditions` = reports with their rappel notes and photos, `ratings`; forecasts are separate, the
viewer refreshes those itself). Each section has a fingerprint, a hash of its content made by the
server only when it ingests the data. Fetch times are not part of the content, so re-ingesting
unchanged data changes nothing, and a section's `updated_at` moves only when its content does.

- `section_stamps` holds the fingerprints a file was built from. The server writes them into every
  export; the viewer stores the server's, it never computes its own.
- `GET /api/manifest?title=` returns the server's fingerprints. Comparing them with a file's says
  which sections are out of date; a file with no stamps (built before they existed) is treated as
  entirely out of date.
- `POST /api/updates {title, have, force}` takes the file's fingerprints (`have`), refreshes the
  canyon from Ropewiki if its data is over 24 hours old (`force` skips that, but never within 5
  minutes of the last check), and returns a job. `GET /api/updates/<job>/delta` gives the rows of
  just the sections that differ (a delta). The viewer replaces that canyon's rows in those
  sections, upgrading the file's schema if needed, and records the new fingerprints.
- The viewer keeps each applied section as a small patch on the device, and applies the patches
  again whenever the file is opened, so a 100 MB file is never rewritten. A patch the file already
  contains, or is newer than, is dropped.

## What the KML says

The converter keeps what the Ropewiki KML itself contains, so the map can match Ropewiki's:

- `routes.color`: the line's own colour from the KML style, as `#rrggbb` (KML stores `aabbggrr`;
  Ropewiki's KMLs use green approach, red descent, yellow exit). NULL when the KML gives none; the
  viewer then colours by `kind`.
- `rappels.kml_name` and `kml_description`: the node's exact name (`R1-R5`, `R10-R12 Sequence`) and
  its description text (tags removed). `rappels.label` stays the normalized label (`R1`, `R10`)
  that wiki rappel lines and condition notes use; the viewer shows `kml_name` on the map.
- `waypoints.description`: the node's description text.

A node that covers a range (`R1-R5`) is attached to its first rappel only. A canyon whose wiki page
lists no rappels (such as KC Canyon) gets one rappel row per KML rappel node.

Version 4 added these columns; `upgrade_geopackage` (and the viewer's reader) cope with files
built before. Canyons already in PostGIS get the fields when they are next ingested (within
24 hours of a request, or sooner by re-running the converter).

## Condition reports

Each `Conditions:` page is one row in `conditions`, keyed by its page title.

- `trip_date` is the `Date` field, ISO formatted. When a report has no `Date`, it falls
  back to the filing date from the title, and `trip_date_source` says which was used.
  Query by date with `ORDER BY trip_date`; the index `(canyon_id, trip_date)` covers this.
- `reported_at` is the filing time from the page title (`-YYYYMMDDHHMMSS`). Its time zone
  is not confirmed. `revised_at` is the UTC revision timestamp from the API.
- Coded fields are split into code and label. `"2 - Thin wetsuit (3mm-4mm)"` gives
  `wetsuit_code=2`, `wetsuit_label="Thin wetsuit"`, `wetsuit_note="3mm-4mm"`. Values
  without a leading code (for example `a2+ - B+ - Deep pools`) keep the whole text in the
  label.
- `general_notes` is the cleaned Comments text outside the rappel notes.
- `condition_rappel_notes` holds one row per rappel label. `R4 & R5: ...` gives two rows.
  The label matches `rappels.label` for the same canyon.
- `condition_photos` holds the `FileList` names in order.
- `raw_template_json` keeps the whole template.

## Cleaned text

`beta.body_wiki` is the section as written. `beta.body_text` is plain text:
templates, comments, refs and file links are removed; `[[A|B]]` and `[url label]` keep
the label; list markers become `- `; HTML entities are decoded. `rappels.notes` is also
cleaned.

`beta.body_html` is the section rendered as HTML for QField's HTML widget. It contains only
`<p>`, `<h4>`, `<ul>`, `<ol>`, `<li>`, `<a href>`, `<b>`, `<i>` and `<br>`. All text is
escaped. Wiki links point to `https://ropewiki.com/`, and bare URLs become links. Templates,
comments, refs and file links are dropped.

## Units and conventions

- Geometry is WGS84 (EPSG:4326), stored as GeoPackage binary (GPKG header + WKB).
  Coordinates are `lon, lat`. The wiki writes `lat, lon`; the converter swaps them.
- Metric columns use metres and kilometres. The converter converts the wiki's imperial
  values, which are set by the `{{Canyon}}` template's `GetNumberUnit` calls:
  - `Longest rappel`, `Approach elevation gain`, `Exit elevation gain`: feet
  - `Hike length`: miles
  - `Fastest/Slowest typical time`: hours
- `Team time` in condition reports is treated as hours. The wiki has no unit field, so
  this is an assumption.
- Dates are ISO 8601 (`YYYY-MM-DD`). Timestamps are UTC.
- `canyons.raw_template_json` keeps every infobox parameter as written, so no value is
  lost if a conversion is wrong.
- Natural keys: `canyons.ropewiki_title` (unique), `conditions.source_title`,
  `ratings.source_title`, `kml_files(canyon_id, file_title)`.

## Known gaps (v1)

- No R-tree spatial index yet. Add `rtree` triggers before the GeoPackage is used for
  bbox queries at scale. PostGIS will use a GiST index instead.
- One KML per page: the converter uses the `Has KML file` semantic property, which holds
  a single URL.
- The `exit` route kind is inferred from the KML name (contains "exit"). Other routes are
  `approach`, `descent` or `unknown` from the existing KML classifier.
- `rappels.anchor_text` is reserved for manual entry and is not filled yet.
- Rappel heights are the first feet or metre value on the rappel line. Check lines with
  more than one measurement.
- Some condition reports have no `Quality`, `Water` or `Wetsuit` value. Those columns are
  NULL.
- Rappel notes are matched by label only. Labels that appear in a report but not on the
  page are stored without a matching rappel row.
- Only Ropewiki data is covered. DEM-derived outputs (ravine spines, rappel candidates,
  detections) are not in the schema yet.
- A `mark` event does not update `rappels.geom`; the viewer applies marks from its own storage.
  The saved events carry them, but nothing reads them back into a rebuilt file.
- Runs in a GeoPackage are written, never read: opening a file does not import its runs.
- The PostGIS store does not hold runs yet: they are recorded on devices and written into a
  GeoPackage by the viewer, and nothing uploads them to the server.
- Forecasts come from a single provider (Open-Meteo).
- The `condition_rappel_notes` and `condition_photos` tables are not registered in
  `gpkg_contents`, so GIS tools do not list them.

## Tile layers

Raster tiles are EPSG:3857 tile tables (GeoPackage 1.4 tiles), one table per layer, so each
can be switched or blended in the viewer.

| Table | Zoom | Source | Format |
|---|---|---|---|
| `tiles_osm` | 13-18 | OSM map rendered to raster (MBTiles input) | PNG |
| `tiles_imagery` | 14-18 | Aerial imagery GeoTIFF (e.g. NAIP) | JPEG where fully covered, PNG at edges |
| `tiles_hillshade` | 14-17 | Hillshade from a 1 m DEM | PNG (grey with alpha) |

Each layer covers the canyon geometry plus a 500 m buffer. `gpkg_contents.description` holds
the attribution. `gpkg_tile_matrix_set` and `gpkg_tile_matrix` describe the zoom levels.

Build (inside `nix develop`). Use `--dry-run` first, since it prints tile counts without writing:

```sh
canyongis-gpkg-tiles runs/canyons.gpkg --imagery naip.tif --dem dem_utm.tif \
    --osm-mbtiles osm.mbtiles --dry-run
canyongis-gpkg-tiles runs/canyons.gpkg --imagery naip.tif --dem dem_utm.tif \
    --osm-mbtiles osm.mbtiles
```

The build stops if it would write more than 200,000 tiles, unless `--max-tiles` is given.

## Tile layer stamps (`tile_stamps`)

Every tile table written by `canyongis-gpkg-tiles` is stamped in `tile_stamps (layer, version,
built_at)`: the version of the code that built it (`tiles.LAYER_VERSIONS`) and when. The table is
not part of the canyon schema; a file without it counts each tile table as version 1, built at its
`gpkg_contents.last_change`.

A layer is out of date when it is missing, its version is below `LAYER_VERSIONS`, or it is over a
year old (`tiles.TILE_MAX_AGE`). **Bump a layer's version in `tiles.LAYER_VERSIONS` whenever its
zoom range, source data, rendering or encoding changes.** The server keeps the same stamps per
canyon (`canyon.tile_builds.layers`, with `attempts` so a layer the area cannot have is not hunted
for again for 30 days, and a failed rebuild is retried after a day). `/api/manifest` reports
`map.layers` and `map.pending`; `/api/updates` accepts the device's `tiles` and returns
`tiles_outdated`, rebuilding stale layers first. The viewer then replaces its file with the
server's current one (runs, events and fixes are stored apart from it and carry over).

## DEM caching on the server

Raw 1 m DEM tiles live in one shared directory (`<build-dir>/dem_sources`), so canyons that are
close together download them once; nothing deletes them yet (a separate cleanup is planned, to run
once the cropped DEMs of the canyons that use them exist). Each canyon also keeps its own cropped,
compressed DEM (`<build-dir>/dems/<slug>.tif`, the area its map covers), with
`canyon.tile_builds.dem_path` and `dem_sources` (the raw tiles it was cut from). A rebuild uses the
cropped DEM and downloads nothing; when only the 3D elevation layer is missing or old, it is added
to a copy of the existing artifact (`tile_build --dem <cropped> --terrain`). Canyons built before
this was kept download their DEM once more on their first rebuild.

# Canyon GeoPackage schema (v1)

Intermediate, portable copy of canyon data. The planned PostGIS store will use the
same table and column names, so the migration is a row-for-row copy.

Code: `src/canyongis/geopackage/` (`schema.py` holds the DDL, `ropewiki_convert.py` the
converter). Build a file with:

```sh
canyongis-ropewiki-gpkg runs/canyons.gpkg "Lytle Creek (Middle Fork)" \
    "https://ropewiki.com/Willow_Creek_Canyon" --cache-dir runs/ropewiki-gpkg/cache
```

Add `--no-kml` to skip KML downloads (no routes, waypoints or rappel geometry).

## Tables

| Table | Type | Geometry | One row per | Source |
|---|---|---|---|---|
| `canyons` | feature | POINT (infobox `Coordinates`) | canyon page | `{{Canyon}}` |
| `routes` | feature | LINESTRING | KML track | page KML |
| `waypoints` | feature | POINT | KML point that is not a rappel | page KML |
| `rappels` | feature | POINT (NULL if no KML anchor) | rappel | wiki rappel lines, KML anchors |
| `sections` | attribute | - | page section | page prose, as wiki and cleaned text |
| `beta_links` | attribute | - | external link | "Beta sites" section |
| `conditions` | attribute | - | condition report | `Conditions:<canyon>-<timestamp>` |
| `condition_rappel_notes` | attribute | - | rappel note in a report | `R1:`, `R4 & R5:` lines in Comments |
| `condition_photos` | attribute | - | photo in a report | `FileList` |
| `ratings` | attribute | - | vote | `Votes:<canyon>/<user>` |
| `kml_files` | attribute | - | KML file | page KML (sha256, URL, download time) |
| `schema_meta` | attribute | - | key | schema version, generator |

GeoPackage bookkeeping tables (`gpkg_spatial_ref_sys`, `gpkg_contents`,
`gpkg_geometry_columns`) are present, and the file sets `application_id` and `user_version`
as GeoPackage 1.4 requires.

Relationships: every child row has `canyon_id` (foreign key, `ON DELETE CASCADE`).
`routes`, `waypoints` and `rappels` also reference the `kml_files` row they came from.

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

`sections.body_wiki` is the section as written. `sections.body_text` is plain text:
templates, comments, refs and file links are removed; `[[A|B]]` and `[url label]` keep
the label; list markers become `- `; HTML entities are decoded. `rappels.notes` is also
cleaned.

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
  detections) are not in v1.

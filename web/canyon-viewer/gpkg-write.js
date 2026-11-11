// Write what the companion recorded (runs, events, GPS fixes) and the freshest weather
// forecasts into the open GeoPackage, then export it. Works in the browser and in Node.
//
// The tables are the schema-version-3 and 5 ones from src/canyongis/geopackage/schema.py (runs,
// forecasts, section stamps) and the version 4 KML columns. Files built before them don't have
// them, so DDL_V3 / DDL_V5 (kept identical to the Python `_DDL_V3` / `_DDL_V5`; a pytest checks)
// and `ensureTables` create whatever is missing. Writing is idempotent: a run's events and fixes are
// replaced as a whole, runs already in the file that this device doesn't have are left alone,
// and a forecast only replaces an older one.
(function (root) {
  "use strict";

  const SCHEMA_VERSION = "5";
  const WGS84 = 4326;

  const DDL_V3 = `
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid     TEXT NOT NULL UNIQUE,
    canyon_title TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    next_index   INTEGER,
    source       TEXT,
    geom         BLOB
);
CREATE TABLE IF NOT EXISTS run_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid        TEXT NOT NULL REFERENCES runs(run_uuid) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    t               TEXT NOT NULL,
    type            TEXT NOT NULL,
    rappel_sequence INTEGER,
    acc             REAL,
    geom            BLOB,
    UNIQUE (run_uuid, seq)
);
CREATE TABLE IF NOT EXISTS run_fixes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL REFERENCES runs(run_uuid) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    t        TEXT NOT NULL,
    acc      REAL,
    geom     BLOB,
    UNIQUE (run_uuid, seq)
);
CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id   INTEGER NOT NULL UNIQUE REFERENCES canyons(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    elevation_m REAL,
    timezone    TEXT
);
CREATE TABLE IF NOT EXISTS forecast_days (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id     INTEGER NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    weather_code    INTEGER,
    temp_max_c      REAL,
    temp_min_c      REAL,
    precip_mm       REAL,
    precip_prob_pct INTEGER,
    wind_max_kmh    REAL,
    gust_max_kmh    REAL,
    sunrise         TEXT,
    sunset          TEXT,
    UNIQUE (forecast_id, date)
);
CREATE INDEX IF NOT EXISTS idx_runs_canyon ON runs (canyon_title);
`;

  const DDL_V5 = `
CREATE TABLE IF NOT EXISTS section_stamps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id  INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    section    TEXT NOT NULL,
    digest     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (canyon_id, section)
);
CREATE TABLE IF NOT EXISTS forecast_hours (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id     INTEGER NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    time            TEXT NOT NULL,
    weather_code    INTEGER,
    temp_c          REAL,
    precip_mm       REAL,
    precip_prob_pct INTEGER,
    wind_kmh        REAL,
    gust_kmh        REAL,
    UNIQUE (forecast_id, time)
);
CREATE TABLE IF NOT EXISTS authors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    canyon_id INTEGER NOT NULL REFERENCES canyons(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    UNIQUE (canyon_id, name)
);
`;

  // Columns added in version 4 (what the KML itself says).
  const COLUMNS_V4 = [
    ["routes", "color", "TEXT"],
    ["waypoints", "description", "TEXT"],
    ["rappels", "kml_name", "TEXT"],
    ["rappels", "kml_description", "TEXT"],
    // attribution (schema version 5)
    ["canyons", "license_name", "TEXT"],
    ["canyons", "license_url", "TEXT"],
    ["canyons", "anonymous_authors", "INTEGER"],
  ];

  // [table, identifier, description, geometry type] and [table, description].
  const FEATURE_TABLES = [
    ["runs", "Runs", "Descents recorded by the companion app (track from GPS fixes)", "LINESTRING"],
    ["run_events", "Run events", "Start, next, back, jump, mark and finish events", "POINT"],
    ["run_fixes", "Run GPS fixes", "GPS positions logged during a run", "POINT"],
  ];
  const ATTRIBUTE_TABLES = [
    ["forecasts", "Weather forecast fetched for each canyon, with its fetch time"],
    ["forecast_days", "Daily forecast values"],
    ["forecast_hours", "Hourly forecast values"],
    ["section_stamps", "Content fingerprint of each data section, so updates can tell what changed"],
    ["authors", "Ropewiki page authors, for attribution"],
  ];
  const HOUR_COLUMNS = [
    "time", "weather_code", "temp_c", "precip_mm", "precip_prob_pct", "wind_kmh", "gust_kmh",
  ];
  const FORECAST_COLUMNS = [
    "date", "weather_code", "temp_max_c", "temp_min_c", "precip_mm", "precip_prob_pct",
    "wind_max_kmh", "gust_max_kmh", "sunrise", "sunset",
  ];

  // ---- geometry blobs: GPKG header (no envelope, little-endian) + WKB, as geometry.py ------

  function header(view, srsId) {
    view.setUint8(0, 0x47); // G
    view.setUint8(1, 0x50); // P
    view.setUint8(2, 0); // version
    view.setUint8(3, 0b0000_0001); // little-endian, no envelope
    view.setInt32(4, srsId, true);
  }

  function pointBlob(lon, lat, srsId = WGS84) {
    const bytes = new Uint8Array(8 + 21);
    const view = new DataView(bytes.buffer);
    header(view, srsId);
    view.setUint8(8, 1);
    view.setUint32(9, 1, true);
    view.setFloat64(13, lon, true);
    view.setFloat64(21, lat, true);
    return bytes;
  }

  function lineBlob(coords, srsId = WGS84) {
    if (coords.length < 2) throw new Error("A line needs at least two points");
    const bytes = new Uint8Array(8 + 9 + 16 * coords.length);
    const view = new DataView(bytes.buffer);
    header(view, srsId);
    view.setUint8(8, 1);
    view.setUint32(9, 2, true);
    view.setUint32(13, coords.length, true);
    coords.forEach(([lon, lat], i) => {
      view.setFloat64(17 + 16 * i, lon, true);
      view.setFloat64(25 + 16 * i, lat, true);
    });
    return bytes;
  }

  // [minX, minY, maxX, maxY] of a blob made by pointBlob / lineBlob.
  function blobEnvelope(blob) {
    const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
    const type = view.getUint32(9, true);
    if (type === 1) {
      const x = view.getFloat64(13, true);
      const y = view.getFloat64(21, true);
      return [x, y, x, y];
    }
    const n = view.getUint32(13, true);
    const box = [Infinity, Infinity, -Infinity, -Infinity];
    for (let i = 0; i < n; i++) {
      const x = view.getFloat64(17 + 16 * i, true);
      const y = view.getFloat64(25 + 16 * i, true);
      box[0] = Math.min(box[0], x);
      box[1] = Math.min(box[1], y);
      box[2] = Math.max(box[2], x);
      box[3] = Math.max(box[3], y);
    }
    return box;
  }

  // ---- schema ---------------------------------------------------------------------------

  const iso = (ms) => new Date(ms).toISOString();

  // One statement per call: the Node driver (better-sqlite3) cannot run a script.
  const statements = (sql) => sql.split(";").map((s) => s.trim()).filter(Boolean);

  function ensureTables(db) {
    for (const statement of [...statements(DDL_V3), ...statements(DDL_V5)]) db.exec(statement);
    for (const [table, column, kind] of COLUMNS_V4) {
      const have = db.run(`SELECT name FROM pragma_table_info('${table}')`).map((r) => r.name);
      if (!have.includes(column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${kind}`);
    }
    for (const [table, identifier, description, geomType] of FEATURE_TABLES) {
      db.exec(
        `INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description, srs_id)
         VALUES (?, 'features', ?, ?, ?)`, [table, identifier, description, WGS84]);
      db.exec(
        "INSERT OR IGNORE INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)",
        [table, geomType, WGS84]);
    }
    for (const [table, description] of ATTRIBUTE_TABLES) {
      db.exec(
        `INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description)
         VALUES (?, 'attributes', ?, ?)`, [table, table, description]);
    }
    db.exec(
      `INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
       ON CONFLICT(key) DO UPDATE SET value = CASE
         WHEN CAST(schema_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)
         THEN excluded.value ELSE schema_meta.value END`, [SCHEMA_VERSION]);
  }

  // Recompute the bounding boxes in gpkg_contents (default: the run tables).
  function refreshExtents(db, stamp, tables = FEATURE_TABLES.map(([table]) => table)) {
    for (const table of tables) {
      let box = null;
      for (const { geom } of db.run(`SELECT geom FROM ${table} WHERE geom IS NOT NULL`)) {
        const e = blobEnvelope(geom);
        box = box ? [Math.min(box[0], e[0]), Math.min(box[1], e[1]),
                     Math.max(box[2], e[2]), Math.max(box[3], e[3])] : e;
      }
      db.exec(
        `UPDATE gpkg_contents SET min_x = ?, min_y = ?, max_x = ?, max_y = ?, last_change = ?
          WHERE table_name = ?`,
        [...(box || [null, null, null, null]), stamp, table]);
    }
  }

  // ---- writing --------------------------------------------------------------------------

  function writeRun(db, run, events, fixes) {
    const byTime = (a, b) => a.t - b.t || (a.id ?? 0) - (b.id ?? 0);
    const sortedFixes = fixes.slice().sort(byTime);
    const sortedEvents = events.slice().sort(byTime);
    const track = sortedFixes.map((f) => [f.lon, f.lat]);
    db.exec("DELETE FROM run_events WHERE run_uuid = ?", [run.id]);
    db.exec("DELETE FROM run_fixes WHERE run_uuid = ?", [run.id]);
    db.exec(
      `INSERT INTO runs (run_uuid, canyon_title, started_at, ended_at, next_index, source, geom)
       VALUES (?, ?, ?, ?, ?, 'canyongis-viewer', ?)
       ON CONFLICT(run_uuid) DO UPDATE SET
         canyon_title = excluded.canyon_title, started_at = excluded.started_at,
         ended_at = excluded.ended_at, next_index = excluded.next_index,
         source = excluded.source, geom = excluded.geom`,
      [run.id, run.canyonId, iso(run.startedAt), run.endedAt ? iso(run.endedAt) : null,
       run.nextIndex ?? null, track.length >= 2 ? lineBlob(track) : null]);
    sortedEvents.forEach((e, seq) => db.exec(
      `INSERT INTO run_events (run_uuid, seq, t, type, rappel_sequence, acc, geom)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      [run.id, seq, iso(e.t), e.type, e.rappelOrder ?? null, e.acc ?? null,
       e.lat !== null && e.lat !== undefined && e.lon !== null && e.lon !== undefined
         ? pointBlob(e.lon, e.lat) : null]));
    sortedFixes.forEach((f, seq) => db.exec(
      "INSERT INTO run_fixes (run_uuid, seq, t, acc, geom) VALUES (?, ?, ?, ?, ?)",
      [run.id, seq, iso(f.t), f.acc ?? null, pointBlob(f.lon, f.lat)]));
  }

  // Replace the canyon's forecast unless the file already holds a newer (or the same) one.
  function writeForecast(db, canyonId, f) {
    const [existing] = db.run("SELECT fetched_at FROM forecasts WHERE canyon_id = ?", [canyonId]);
    if (existing && existing.fetched_at >= f.fetched_at) return false;
    // sql.js leaves foreign keys off, so ON DELETE CASCADE does not run: delete children first.
    for (const child of ["forecast_days", "forecast_hours"]) {
      db.exec(
        `DELETE FROM ${child} WHERE forecast_id IN (SELECT id FROM forecasts WHERE canyon_id = ?)`,
        [canyonId]);
    }
    db.exec("DELETE FROM forecasts WHERE canyon_id = ?", [canyonId]);
    const { lastInsertRowid } = db.exec(
      `INSERT INTO forecasts (canyon_id, source, fetched_at, latitude, longitude, elevation_m,
                              timezone) VALUES (?, ?, ?, ?, ?, ?, ?)`,
      [canyonId, f.source, f.fetched_at, f.latitude, f.longitude, f.elevation_m ?? null,
       f.timezone ?? null]);
    for (const day of f.days) {
      db.exec(
        `INSERT INTO forecast_days (forecast_id, ${FORECAST_COLUMNS.join(", ")})
         VALUES (?, ${FORECAST_COLUMNS.map(() => "?").join(", ")})`,
        [lastInsertRowid, ...FORECAST_COLUMNS.map((c) => day[c] ?? null)]);
    }
    for (const hour of f.hours || []) {
      db.exec(
        `INSERT INTO forecast_hours (forecast_id, ${HOUR_COLUMNS.join(", ")})
         VALUES (?, ${HOUR_COLUMNS.map(() => "?").join(", ")})`,
        [lastInsertRowid, ...HOUR_COLUMNS.map((c) => hour[c] ?? null)]);
    }
    return true;
  }

  // Write the companion's data into `db` (from gpkg.openDatabase) in one transaction.
  //   data: { runs, events, fixes, forecasts }  (forecasts: {canyon title: record})
  // Runs and forecasts for canyons that are not in the file are skipped, so no row lacks its
  // canyon. Returns counts of what was written.
  function saveInto(db, data, now = Date.now()) {
    const canyons = new Map(
      db.run("SELECT id, ropewiki_title FROM canyons").map((c) => [c.ropewiki_title, c.id]));
    const summary = { runs: 0, events: 0, fixes: 0, forecasts: 0, skippedRuns: 0 };
    const group = (rows) => {
      const out = new Map();
      for (const row of rows) {
        if (!out.has(row.runId)) out.set(row.runId, []);
        out.get(row.runId).push(row);
      }
      return out;
    };
    const eventsByRun = group(data.events || []);
    const fixesByRun = group(data.fixes || []);
    db.transaction(() => {
      ensureTables(db);
      for (const run of data.runs || []) {
        if (!canyons.has(run.canyonId)) {
          summary.skippedRuns += 1;
          continue;
        }
        const events = eventsByRun.get(run.id) || [];
        const fixes = fixesByRun.get(run.id) || [];
        writeRun(db, run, events, fixes);
        summary.runs += 1;
        summary.events += events.length;
        summary.fixes += fixes.length;
      }
      for (const [title, f] of Object.entries(data.forecasts || {})) {
        if (canyons.has(title) && writeForecast(db, canyons.get(title), f)) summary.forecasts += 1;
      }
      refreshExtents(db, iso(now));
    });
    return summary;
  }

  const api = {
    DDL_V3, DDL_V5, SCHEMA_VERSION, pointBlob, lineBlob, blobEnvelope, ensureTables, refreshExtents,
    saveInto,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gpkgWrite = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

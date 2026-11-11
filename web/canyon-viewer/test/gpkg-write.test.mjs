import { after, test } from "node:test";
import { createServer } from "node:http";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const gpkg = require("../gpkg.js");
const write = require("../gpkg-write.js");
const forecast = require("../forecast.js");
const lib = require("@ngageoint/geopackage");
// The page runs on sql.js (in-memory, exports straight to bytes); Node would pick
// better-sqlite3, whose export reads a WAL file. Use sql.js so these tests take the browser path.
lib.Context.setupCustomContext(lib.SqljsAdapter, lib.CanvasKitCanvasAdapter);
// sql.js fetches its WASM over HTTP when it doesn't see Node, so serve it from a local port.
const wasm = readFileSync(
  fileURLToPath(new URL("../node_modules/@ngageoint/geopackage/dist/sql-wasm.wasm", import.meta.url)));
const server = createServer((req, res) => {
  res.writeHead(200, { "content-type": "application/wasm" });
  res.end(wasm);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
lib.setSqljsWasmLocateFile((name) => `http://127.0.0.1:${server.address().port}/${name}`);
after(() => server.close());
const fixtureBytes = readFileSync(fileURLToPath(new URL("./fixtures/lytle.gpkg", import.meta.url)));
const payload = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../../tests/fixtures/open-meteo.json", import.meta.url)), "utf8"));

const TITLE = "Lytle Creek (Middle Fork)";
const T0 = Date.UTC(2026, 10, 9, 15, 0, 0);

function sampleData() {
  const run = { id: "run-1", canyonId: TITLE, startedAt: T0, endedAt: T0 + 3600e3, nextIndex: 2 };
  const ev = (id, t, type, extra = {}) => ({
    id, runId: "run-1", canyonId: TITLE, t, type, rappelId: null, rappelOrder: null,
    lat: null, lon: null, acc: null, ...extra,
  });
  return {
    runs: [run, { id: "elsewhere", canyonId: "Not in this file", startedAt: T0, endedAt: null }],
    events: [
      ev(3, T0 + 1800e3, "mark", { rappelId: "1", rappelOrder: 1, lat: 34.2487, lon: -117.5768, acc: 4 }),
      ev(1, T0, "start"),
      ev(2, T0 + 60e3, "next", { rappelId: "1", rappelOrder: 1, lat: 34.25, lon: -117.577, acc: 6 }),
    ],
    fixes: [
      { id: 2, runId: "run-1", t: T0 + 3e3, lat: 34.2501, lon: -117.5771, acc: 5 },
      { id: 1, runId: "run-1", t: T0, lat: 34.25, lon: -117.577, acc: 8 },
      { id: 3, runId: "run-1", t: T0 + 6e3, lat: 34.2502, lon: -117.5772, acc: 5 },
    ],
    forecasts: { [TITLE]: forecast.parseForecast(payload, "2026-11-09T12:00:00Z") },
  };
}

const ALL = ["canyons", "conditions", "rappels", "runs", "run_events", "run_fixes", "forecasts",
  "forecast_days"];
const counts = (db, tables = ALL) => Object.fromEntries(
  tables.map((t) => [t, db.run(`SELECT count(*) AS n FROM ${t}`)[0].n]));

test("saves runs, events, fixes and the forecast, and the export reopens with them", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  const before = counts(db, ["canyons", "conditions", "rappels"]);
  const summary = write.saveInto(db, sampleData());
  assert.deepEqual(summary, { runs: 1, events: 3, fixes: 3, forecasts: 1, skippedRuns: 1 });

  const exported = await db.export();
  const back = await gpkg.openDatabase(lib, exported);
  const after = counts(back);
  assert.equal(after.canyons, before.canyons);
  assert.equal(after.conditions, before.conditions);
  assert.equal(after.rappels, before.rappels);
  assert.deepEqual([after.runs, after.run_events, after.run_fixes], [1, 3, 3]);
  assert.deepEqual([after.forecasts, after.forecast_days], [1, 3]);

  const [run] = back.run("SELECT * FROM runs");
  assert.equal(run.run_uuid, "run-1");
  assert.equal(run.started_at, "2026-11-09T15:00:00.000Z");
  assert.equal(run.canyon_title, TITLE);
  const track = back.decode(run.geom);
  assert.equal(track.type, "LineString");
  assert.deepEqual(track.coordinates[0], [-117.577, 34.25]); // fixes ordered by time

  const events = back.run("SELECT * FROM run_events ORDER BY seq");
  assert.deepEqual(events.map((e) => e.type), ["start", "next", "mark"]);
  assert.equal(events[0].geom, null);
  assert.equal(events[2].rappel_sequence, 1);
  assert.deepEqual(back.decode(events[2].geom).coordinates, [-117.5768, 34.2487]);
  assert.equal(back.run("SELECT count(*) AS n FROM run_fixes WHERE geom IS NULL")[0].n, 0);

  const registered = back.run(
    "SELECT table_name, data_type, min_x FROM gpkg_contents WHERE table_name LIKE 'run%'");
  assert.deepEqual(registered.map((r) => r.table_name).sort(), ["run_events", "run_fixes", "runs"]);
  assert.ok(registered.every((r) => r.data_type === "features" && r.min_x !== null));
  assert.equal(back.run("SELECT value FROM schema_meta WHERE key = 'schema_version'")[0].value, "5");

  const detail = gpkg.loadCanyon(back, back.run("SELECT id FROM canyons")[0].id);
  assert.equal(detail.forecast.fetched_at, "2026-11-09T12:00:00Z");
  assert.equal(detail.forecast.days.length, 3);
  assert.equal(detail.forecast.days[2].weather_code, 95);
  assert.equal(detail.forecast.timezone, "America/Los_Angeles");
});

test("the open database still works after export", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  write.saveInto(db, sampleData());
  await db.export();
  assert.equal(gpkg.listCanyons(db).length, 1);
  assert.ok(gpkg.loadCanyon(db, 1).rappels.length > 0);
  assert.ok(gpkg.listTileLayers(db).length > 0);
  assert.ok(gpkg.getTile(db, gpkg.listTileLayers(db)[0].table, 0, 0, 0) !== undefined);
});

test("saving again does not duplicate rows and keeps runs from other devices", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  write.saveInto(db, sampleData());
  // A run another phone saved earlier lives in the file but not in this device's data.
  write.saveInto(db, {
    runs: [{ id: "other-phone", canyonId: TITLE, startedAt: T0 - 864e5, endedAt: null }],
    events: [], fixes: [],
  });
  const data = sampleData();
  data.fixes.push({ id: 4, runId: "run-1", t: T0 + 9e3, lat: 34.2503, lon: -117.5773, acc: 5 });
  const again = write.saveInto(db, data);
  assert.equal(again.forecasts, 0); // same fetch time: not replaced
  const bytes = await db.export();
  const back = await gpkg.openDatabase(lib, bytes);
  const n = counts(back);
  assert.deepEqual([n.runs, n.run_events, n.run_fixes, n.forecasts, n.forecast_days], [2, 3, 4, 1, 3]);
  assert.equal(back.run("SELECT count(*) AS n FROM runs WHERE geom IS NULL")[0].n, 1);
});

test("a newer forecast replaces an older one, an older one does not", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  const at = (iso) => ({ forecasts: { [TITLE]: forecast.parseForecast(payload, iso) } });
  assert.equal(write.saveInto(db, at("2026-11-09T12:00:00Z")).forecasts, 1);
  assert.equal(write.saveInto(db, at("2026-11-09T11:00:00Z")).forecasts, 0);
  assert.equal(write.saveInto(db, at("2026-11-10T08:30:00Z")).forecasts, 1);
  assert.equal(db.run("SELECT fetched_at FROM forecasts")[0].fetched_at, "2026-11-10T08:30:00Z");
  assert.equal(db.run("SELECT count(*) AS n FROM forecast_days")[0].n, 3);
});

test("saving and exporting twice on the same open database keeps working", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  write.saveInto(db, sampleData());
  const first = await db.export();
  const data = sampleData();
  data.fixes.push({ id: 4, runId: "run-1", t: T0 + 9e3, lat: 34.2503, lon: -117.5773, acc: 5 });
  assert.equal(write.saveInto(db, data).fixes, 4); // sql.js export() reopened the database
  const second = await db.export();
  const n1 = counts(await gpkg.openDatabase(lib, first));
  const n2 = counts(await gpkg.openDatabase(lib, second));
  assert.deepEqual([n1.runs, n1.run_events, n1.run_fixes, n1.forecasts], [1, 3, 3, 1]);
  assert.deepEqual([n2.runs, n2.run_events, n2.run_fixes, n2.forecasts, n2.forecast_days],
    [1, 3, 4, 1, 3]);
  assert.equal(gpkg.loadCanyon(db, 1).forecast.days.length, 3);
});

test("a failed save leaves the database as it was", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  const snapshot = () => JSON.stringify([
    db.run("SELECT name FROM sqlite_master ORDER BY name").map((r) => r.name),
    db.run("SELECT table_name FROM gpkg_contents ORDER BY table_name").map((r) => r.table_name),
    db.run("SELECT value FROM schema_meta WHERE key = 'schema_version'"),
  ]);
  const before = snapshot();
  const data = sampleData();
  data.fixes[0].t = "not a time"; // makes the insert throw part-way through the save
  assert.throws(() => write.saveInto(db, data));
  assert.equal(snapshot(), before);
});

test("point and line blobs have the Python writer's layout", () => {
  const p = write.pointBlob(-117.57694, 34.2533);
  const view = new DataView(p.buffer);
  assert.equal(Buffer.from(p.slice(0, 13)).toString("hex"), "475000" + "01" + "e6100000" + "01" + "01000000");
  assert.equal(view.getFloat64(13, true), -117.57694);
  assert.equal(view.getFloat64(21, true), 34.2533);
  const line = write.lineBlob([[0, 1], [2, 3]]);
  assert.equal(line.length, 8 + 9 + 32);
  assert.deepEqual(write.blobEnvelope(line), [0, 1, 2, 3]);
  assert.deepEqual(write.blobEnvelope(p), [-117.57694, 34.2533, -117.57694, 34.2533]);
});

test("saving never lowers the schema version, and raises an older one to 5", async () => {
  const version = (d) => d.run("SELECT value FROM schema_meta WHERE key = 'schema_version'")[0].value;
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  db.exec("UPDATE schema_meta SET value = '9' WHERE key = 'schema_version'");
  write.saveInto(db, sampleData());
  assert.equal(version(db), "9");
  const old = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  old.exec("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'");
  write.saveInto(old, sampleData());
  assert.equal(version(old), "5");
});

test("hourly forecast values are saved, read back, and replaced rather than added to", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  write.saveInto(db, sampleData());
  const newer = { forecasts: { [TITLE]: forecast.parseForecast(payload, "2026-11-10T08:30:00Z") } };
  assert.equal(write.saveInto(db, newer).forecasts, 1);
  const back = await gpkg.openDatabase(lib, await db.export());
  assert.equal(back.run("SELECT count(*) AS n FROM forecast_hours")[0].n, 72);
  const f = gpkg.loadCanyon(back, 1).forecast;
  assert.equal(f.hours.length, 72);
  assert.deepEqual(f.hours[0], { time: "2026-11-09T00:00", weather_code: f.hours[0].weather_code,
    temp_c: f.hours[0].temp_c, precip_mm: 0, precip_prob_pct: 0,
    wind_kmh: f.hours[0].wind_kmh, gust_kmh: f.hours[0].gust_kmh });
  assert.equal(f.hours.find((h) => h.time === "2026-11-11T14:00").weather_code, 95);
});

test("a file with no hourly table still loads its forecast", async () => {
  const db = await gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
  write.saveInto(db, sampleData());
  db.exec("DROP TABLE forecast_hours");
  assert.deepEqual(gpkg.loadCanyon(db, 1).forecast.hours, []);
});

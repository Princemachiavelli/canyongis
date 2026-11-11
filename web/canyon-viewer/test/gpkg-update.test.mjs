import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const gpkg = require("../gpkg.js");
const update = require("../gpkg-update.js");
const lib = require("@ngageoint/geopackage");
// The page runs on sql.js; use it here too (see gpkg-write.test.mjs).
lib.Context.setupCustomContext(lib.SqljsAdapter, lib.CanvasKitCanvasAdapter);
const wasm = readFileSync(
  fileURLToPath(new URL("../node_modules/@ngageoint/geopackage/dist/sql-wasm.wasm", import.meta.url)));
const server = createServer((req, res) => { res.writeHead(200, { "content-type": "application/wasm" }); res.end(wasm); });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
lib.setSqljsWasmLocateFile((name) => `http://127.0.0.1:${server.address().port}/${name}`);
import { after } from "node:test";
after(() => server.close());

const fixtureBytes = readFileSync(fileURLToPath(new URL("./fixtures/lytle.gpkg", import.meta.url)));
// A delta made by the real server code (test/make_delta_fixture.py) after some edits on "Ropewiki".
const { before, manifest, delta } = JSON.parse(readFileSync(
  fileURLToPath(new URL("./fixtures/delta.json", import.meta.url)), "utf8"));
const TITLE = "Lytle Creek (Middle Fork)";
const open = () => gpkg.openDatabase(lib, new Uint8Array(fixtureBytes));
const count = (db, sql, params = []) => db.run(sql, params)[0].n;

test("a file from before fingerprints existed has none, so every section is unknown", async () => {
  const db = await open();
  const stamps = update.readStamps(db, 1);
  assert.deepEqual(stamps, {});
  const rows = update.compare(stamps, manifest);
  assert.deepEqual(rows.map((r) => r.state), Array(6).fill("unknown"));
  assert.deepEqual(rows.map((r) => r.section), Object.keys(manifest.sections));
  assert.equal(rows.find((r) => r.section === "canyon").label, "Canyon details");
  assert.equal(update.needsUpdate(rows).length, 6);
  assert.deepEqual(update.haveDigests(stamps), {});
});

test("applying a delta replaces just those sections and upgrades the file", async () => {
  const db = await open();
  const ratingsBefore = db.run("SELECT rater, rating FROM ratings ORDER BY id");
  const tilesBefore = count(db, "SELECT count(*) AS n FROM tiles_imagery");
  const result = update.applyDelta(db, delta);
  assert.deepEqual(result.applied.sort(), Object.keys(manifest.sections).sort());

  const detail = gpkg.loadCanyon(db, 1);
  assert.equal(detail.canyon.region, "Changed Region"); // canyon row updated in place
  assert.equal(detail.canyon.id, 1);
  assert.equal(detail.canyon.licenseName, "Creative Commons Attribution Non-Commercial Share Alike");
  assert.equal(detail.canyon.anonymousAuthors, 2);
  assert.deepEqual(detail.authors, ["Dangel"]);
  assert.equal(detail.rappels.find((r) => r.label === "R1").height_m, 99);
  assert.ok(detail.links.some((l) => l.url === "https://example.org/new"));
  assert.equal(detail.conditions.length, 1); // the other report was removed on the server
  const [report] = detail.conditions;
  assert.equal(report.rappelNotes.length, 5); // notes and photos follow their new report row
  assert.equal(report.photos.length, 2);
  assert.equal(detail.ratings[0].rating, 1);
  assert.equal(detail.sections.length, 8); // the description is unchanged

  // links between rows survive the new ids
  assert.equal(count(db, `SELECT count(*) AS n FROM condition_rappel_notes n
                          JOIN conditions c ON c.id = n.report_id`), 5);
  assert.equal(count(db, "SELECT count(*) AS n FROM conditions"), 1);
  assert.equal(count(db, "SELECT count(*) AS n FROM tiles_imagery"), tilesBefore);
  assert.ok(ratingsBefore.length >= 1);

  // the file now holds the server's fingerprints, and compares as current
  const stamps = update.readStamps(db, 1);
  assert.deepEqual(update.haveDigests(stamps),
    Object.fromEntries(Object.entries(manifest.sections).map(([k, v]) => [k, v.digest])));
  assert.ok(update.compare(stamps, manifest).every((r) => r.state === "current"));
  assert.equal(update.needsUpdate(update.compare(stamps, manifest)).length, 0);
  assert.equal(db.run("SELECT value FROM schema_meta WHERE key = 'schema_version'")[0].value, "5");
});

test("only the sections in the delta change", async () => {
  const db = await open();
  const partial = { title: TITLE, sections: { links: delta.sections.links } };
  const result = update.applyDelta(db, partial);
  assert.deepEqual(result.applied, ["links"]);
  const detail = gpkg.loadCanyon(db, 1);
  assert.ok(detail.links.some((l) => l.url === "https://example.org/new"));
  assert.notEqual(detail.canyon.region, "Changed Region");
  assert.equal(detail.conditions.length, 2);
  const stamps = update.readStamps(db, 1);
  assert.deepEqual(Object.keys(stamps), ["links"]);
  const rows = update.compare(stamps, manifest);
  assert.deepEqual(rows.filter((r) => r.state === "current").map((r) => r.section), ["links"]);
});

test("applying a delta twice gives the same file", async () => {
  const db = await open();
  update.applyDelta(db, delta);
  const once = JSON.stringify([
    db.run("SELECT region FROM canyons"), db.run("SELECT count(*) AS n FROM links"),
    db.run("SELECT count(*) AS n FROM conditions"), db.run("SELECT count(*) AS n FROM rappels"),
    db.run("SELECT count(*) AS n FROM section_stamps"), db.run("SELECT count(*) AS n FROM authors")]);
  update.applyDelta(db, delta);
  const twice = JSON.stringify([
    db.run("SELECT region FROM canyons"), db.run("SELECT count(*) AS n FROM links"),
    db.run("SELECT count(*) AS n FROM conditions"), db.run("SELECT count(*) AS n FROM rappels"),
    db.run("SELECT count(*) AS n FROM section_stamps"), db.run("SELECT count(*) AS n FROM authors")]);
  assert.equal(twice, once);
});

test("a delta for a canyon the file does not hold changes nothing", async () => {
  const db = await open();
  const result = update.applyDelta(db, { ...delta, title: "Some Other Canyon" });
  assert.deepEqual(result, { applied: [], rows: 0 });
  assert.equal(gpkg.loadCanyon(db, 1).canyon.region, "Lytle Creek Area");
});

test("a failed update leaves the file as it was", async () => {
  const db = await open();
  const bad = structuredClone(delta);
  bad.sections.links.tables[0].rows[0][bad.sections.links.tables[0].columns.indexOf("url")] = null; // NOT NULL
  assert.throws(() => update.applyDelta(db, { title: TITLE, sections: { canyon: delta.sections.canyon, links: bad.sections.links } }));
  assert.equal(gpkg.loadCanyon(db, 1).canyon.region, "Lytle Creek Area"); // the canyon section was rolled back too
  assert.equal(db.run("SELECT 1 FROM sqlite_master WHERE name = 'section_stamps'").length, 0);
});

test("names in a delta are checked before they reach SQL", async () => {
  const db = await open();
  for (const mutate of [
    (d) => { d.sections.links.tables[0].name = "links; DROP TABLE canyons"; },
    (d) => { d.sections.links.tables[0].columns.push("url) VALUES (1); --"); },
    (d) => { d.sections.links.tables[0].fks = { "canyon_id; x": "canyons" }; },
  ]) {
    const bad = structuredClone({ title: TITLE, sections: { links: delta.sections.links } });
    mutate(bad);
    assert.throws(() => update.applyDelta(db, bad), /Unexpected name/);
  }
  assert.throws(() => update.applyDelta(db, { title: TITLE, sections: { "x y": delta.sections.links } }), /Unexpected section name/);
  assert.equal(count(db, "SELECT count(*) AS n FROM canyons"), 1);
});

test("stored patches are applied when newer, and reported obsolete when the file has them", async () => {
  const patches = update.patchesFromDelta(delta);
  assert.equal(patches.length, 6);
  assert.equal(patches[0].id, update.patchId(TITLE, patches[0].section));
  const db = await open();
  const first = update.applyStoredPatches(db, patches);
  assert.equal(first.applied.length, 6);
  assert.deepEqual(first.obsolete, []);
  assert.equal(gpkg.loadCanyon(db, 1).canyon.region, "Changed Region");
  const again = update.applyStoredPatches(db, patches);
  assert.deepEqual(again.applied, []);
  assert.equal(again.obsolete.length, 6);
  // a patch for another canyon is left alone
  const other = update.patchesFromDelta({ ...delta, title: "Other" });
  assert.deepEqual(update.applyStoredPatches(db, other), { applied: [], obsolete: [] });
  // a file that is already newer than a patch makes it obsolete without applying it
  const older = patches.map((p) => ({ ...p, digest: "different", updated_at: "2000-01-01T00:00:00Z" }));
  assert.deepEqual(update.applyStoredPatches(db, older).applied, []);
  assert.equal(update.applyStoredPatches(db, older).obsolete.length, 6);
});

test("the delta fixture matches what the server says changed", () => {
  const changed = Object.keys(manifest.sections).filter(
    (s) => manifest.sections[s].digest !== before.sections[s].digest);
  assert.deepEqual(changed.sort(), ["canyon", "conditions", "geometry", "links", "ratings"]);
});

test("tile layers: stamps are read from a file and compared with the server's", async () => {
  const db = await open();
  const held = update.readTileStamps(db);
  assert.ok(Object.keys(held).length > 0);
  for (const stamp of Object.values(held)) assert.equal(stamp.version, 1); // no tile_stamps table yet
  const server = {
    tiles_imagery: { version: 1, built_at: held.tiles_imagery.built_at },
    tiles_terrain: { version: 1, built_at: "2026-11-10T00:00:00Z" },
  };
  assert.deepEqual(update.tileUpdates(held, server), [{ layer: "tiles_terrain", reason: "missing" }]);
  server.tiles_imagery = { version: 2, built_at: held.tiles_imagery.built_at };
  assert.deepEqual(update.tileUpdates(held, server).map((t) => t.reason), ["version", "missing"]);
  server.tiles_imagery = { version: 1, built_at: "2099-01-01T00:00:00Z" };
  assert.equal(update.tileUpdates(held, server)[0].reason, "newer");
  assert.deepEqual(update.tileUpdates(held, {}), []);
  assert.deepEqual(update.tileUpdates(held, undefined), []);
});

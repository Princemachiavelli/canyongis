// Run with: npm test (from web/canyon-viewer, after npm install)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const gpkg = require("../gpkg.js");
const geopackageLib = require("@ngageoint/geopackage");
const fixture = fileURLToPath(new URL("./fixtures/lytle.gpkg", import.meta.url));

const db = await gpkg.openDatabase(geopackageLib, readFileSync(fixture));

test("decodes point geometry from the canyon row", () => {
  const [canyon] = gpkg.listCanyons(db);
  assert.equal(canyon.title, "Lytle Creek (Middle Fork)");
  assert.deepEqual(canyon.lon.toFixed(5), "-117.57694");
  assert.deepEqual(canyon.lat.toFixed(4), "34.2533");
  assert.equal(canyon.technicalRating, 3);
});

test("decodes linestring routes and rappel points", () => {
  const detail = gpkg.loadCanyon(db, 1);
  assert.equal(detail.routes.length, 1);
  assert.equal(detail.routes[0].line.type, "LineString");
  assert.equal(detail.routes[0].line.coordinates.length, 3);
  const r1 = detail.rappels.find((r) => r.label === "R1");
  assert.deepEqual(r1.point.coordinates.map((v) => Number(v.toFixed(4))), [-117.5768, 34.2487]);
  assert.equal(detail.rappels.find((r) => r.label === "R2").point, null);
});

test("conditions are newest first, with notes and photos", () => {
  const detail = gpkg.loadCanyon(db, 1);
  assert.deepEqual(detail.conditions.map((c) => c.trip_date), ["2026-09-20", "2014-06-14"]);
  const latest = detail.conditions[0];
  assert.equal(latest.quality_label, "Ok");
  assert.equal(latest.water_label, "Very Low");
  assert.deepEqual(latest.rappelNotes.map((n) => n.rappel_label), ["R1", "R2", "R3", "R4", "R5"]);
  assert.equal(latest.photos.length, 2);
});

test("sections, links and ratings load", () => {
  const detail = gpkg.loadCanyon(db, 1);
  assert.equal(detail.sections[0].heading, "Introduction");
  assert.ok(detail.sections.some((s) => s.body_html.includes("<p>")));
  assert.ok(detail.links.length > 0);
  assert.equal(detail.ratings[0].rater, "Demo");
});

test("unknown canyon id returns null", () => {
  assert.equal(gpkg.loadCanyon(db, 999), null);
});

test.after(() => db.close());

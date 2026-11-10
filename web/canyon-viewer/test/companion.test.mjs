// Ported from canyon-companion's core/geo.test.ts and core/progress.test.ts (same fixtures),
// plus tests for the GeoPackage-specific helpers. Run with: npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const c = require("../companion.js");

// Due north along lon -111, ten points 0.001 deg (~111.2 m) apart: ~1001 m. Same fixture as
// canyon-companion's pipeline/tests/conftest.py NORTH_SOUTH.
const north = Array.from({ length: 10 }, (_, i) => [-111.0, 40.0 + 0.001 * i]);
const track = new c.TrackIndex(north);
const fix = (lat, lon = -111.0, acc = 5) => ({ lon, lat, acc, t: 0 });
const near = (a, b, tol = 3) => assert.ok(Math.abs(a - b) <= tol, `${a} vs ${b}`);
const bundled = { location: { lon: -111.0, lat: 40.006 }, chainageM: 667 };

test("length agrees with haversine", () => {
  const hav = c.distanceM([-111.0, 40.0], [-111.0, 40.009]);
  assert.ok(Math.abs(track.lengthM - hav) / hav < 1e-3);
  assert.ok(Math.abs(track.lengthM - 1001) < 5);
});

test("projection matches the pipeline's snap fixture", () => {
  const p = track.project(-111.0 + 0.0005, 40.0045);
  assert.ok(Math.abs(p.chainageM - 500.4) < 2, `chainage ${p.chainageM}`);
  assert.ok(Math.abs(p.offsetM - 42.6) < 2, `offset ${p.offsetM}`);
  assert.ok(Math.abs(p.lon - -111.0) < 1e-6);
});

test("projection clamps to the ends", () => {
  assert.equal(track.project(-111.0, 39.99).chainageM, 0);
  assert.ok(Math.abs(track.project(-111.0, 40.02).chainageM - track.lengthM) < 1e-6);
});

test("projection follows a bend instead of cutting the corner", () => {
  const lat1 = 40.0 + 500 / 111_195;
  const lon1 = -111.0 + 500 / (111_195 * Math.cos((lat1 * Math.PI) / 180));
  const idx = new c.TrackIndex([[-111.0, 40.0], [-111.0, lat1], [lon1, lat1]]);
  const p = idx.project(lon1, lat1);
  assert.ok(Math.abs(p.chainageM - 1000) < 5);
  assert.ok(p.offsetM < 1);
});

test("a one-point track is rejected", () => {
  assert.throws(() => new c.TrackIndex([[-111, 40]]));
});

test("along-track distance when on the track", () => {
  const d = c.distanceToRappel(track, fix(40.002), c.resolveTarget(track, bundled));
  assert.equal(d.kind, "along-track");
  near(d.meters, 667 - 222.4);
});

test("negative distance once the rappel has been passed", () => {
  const d = c.distanceToRappel(track, fix(40.008), c.resolveTarget(track, bundled));
  assert.equal(d.kind, "along-track");
  assert.ok(d.meters < 0);
});

test("straight-line distance when far off the track", () => {
  const d = c.distanceToRappel(track, fix(40.002, -111.0 + 0.0065), c.resolveTarget(track, bundled));
  assert.equal(d.kind, "straight");
  assert.ok(d.meters > 560);
});

test("poor accuracy widens the on-track window", () => {
  const d = c.distanceToRappel(track, fix(40.002, -111.0 + 0.0025, 150), c.resolveTarget(track, bundled));
  assert.equal(d.kind, "along-track");
});

test("unknown rappel position gives no distance", () => {
  const target = c.resolveTarget(track, { location: null, chainageM: null });
  assert.equal(target.position, null);
  assert.equal(c.distanceToRappel(track, fix(40.002), target), null);
});

test("a recorded position beats the bundled one", () => {
  const target = c.resolveTarget(track, bundled, { lon: -111.0, lat: 40.004 });
  near(target.chainageM, 444.8);
  near(c.distanceToRappel(track, fix(40.002), target).meters, 444.8 - 222.4);
});

test("a recorded position fills in a rappel the file never located", () => {
  const target = c.resolveTarget(track, { location: null, chainageM: null }, { lon: -111.0, lat: 40.003 });
  assert.ok(target.position && target.chainageM !== null);
});

test("chainage is derived when the file has a location but no chainage", () => {
  const target = c.resolveTarget(track, { location: { lon: -111.0, lat: 40.006 }, chainageM: null });
  near(target.chainageM, 667.1);
});

test("without a track every distance is straight-line", () => {
  const target = c.resolveTarget(null, bundled);
  assert.equal(target.chainageM, 667);
  const onLine = c.distanceToRappel(null, fix(40.002), c.resolveTarget(null, { location: bundled.location }));
  assert.equal(onLine.kind, "straight");
  near(onLine.meters, 445, 5);
  assert.equal(c.resolveTarget(null, { location: null }, { lon: -111, lat: 40 }).chainageM, null);
});

const line = (n) => ({ type: "LineString", coordinates: Array.from({ length: n }, (_, i) => [-111, 40 + i * 0.001]) });

test("pickTrack prefers the longest descent route and never uses approach or exit", () => {
  const routes = [
    { kind: "approach", name: "walk in", line: line(50) },
    { kind: "descent", name: "short", line: line(3) },
    { kind: "descent", name: "long", line: line(9) },
    { kind: "exit", name: "out", line: line(20) },
  ];
  assert.equal(c.pickTrack(routes).name, "long");
  assert.equal(c.pickTrack(routes).coordinates.length, 9);
});

test("pickTrack falls back to an unknown route, else null", () => {
  assert.equal(c.pickTrack([{ kind: "unknown", name: "x", line: line(4) }]).kind, "unknown");
  assert.equal(c.pickTrack([{ kind: "approach", name: "x", line: line(4) }]), null);
  assert.equal(c.pickTrack([{ kind: "descent", name: "one point", line: line(1) }]), null);
  assert.equal(c.pickTrack([{ kind: "descent", name: "no geometry", line: null }]), null);
  assert.equal(c.pickTrack([]), null);
});

test("MultiLineString routes are flattened", () => {
  const multi = { type: "MultiLineString", coordinates: [[[0, 0], [0, 1]], [[0, 2], [0, 3]]] };
  assert.equal(c.lineCoordinates(multi).length, 4);
  assert.equal(c.lineCoordinates({ type: "Point", coordinates: [0, 0] }), null);
});

test("planRappels orders by sequence and keeps unlocated rappels", () => {
  const plan = c.planRappels([
    { sequence: 2, label: "R2", height_m: 24, notes: "n", point: null },
    { sequence: 1, label: "R1", height_m: 10, notes: null, point: { coordinates: [-117.5, 34.2] } },
  ]);
  assert.deepEqual(plan.map((r) => r.id), ["1", "2"]);
  assert.deepEqual(plan[0].location, { lon: -117.5, lat: 34.2 });
  assert.equal(plan[1].location, null);
  assert.equal(plan[1].order, 2);
});

test("events carry the fix, or the clock when there is none", () => {
  const run = { id: "r1", canyonId: "Lytle" };
  const withFix = c.makeEvent(run, "mark", { lon: 1, lat: 2, acc: 3, t: 99 }, { id: "1", order: 1 });
  assert.deepEqual(withFix, {
    runId: "r1", canyonId: "Lytle", t: 99, type: "mark", rappelId: "1", rappelOrder: 1,
    lat: 2, lon: 1, acc: 3,
  });
  const noFix = c.makeEvent(run, "next", null, null, 5);
  assert.equal(noFix.t, 5);
  assert.equal(noFix.lat, null);
  assert.equal(noFix.rappelId, null);
});

test("the latest mark per rappel wins, across runs", () => {
  const mark = (t, id, lat) => ({ type: "mark", t, rappelId: id, lat, lon: -111, acc: 4 });
  const got = c.recordedFromMarks([
    mark(30, "1", 40.3), mark(10, "1", 40.1), mark(20, "2", 40.2),
    { type: "next", t: 40, rappelId: "1", lat: 41, lon: -111, acc: 1 },
    { type: "mark", t: 50, rappelId: "3", lat: null, lon: null, acc: null },
  ]);
  assert.equal(got.get("1").lat, 40.3);
  assert.equal(got.get("2").lat, 40.2);
  assert.equal(got.has("3"), false);
});

test("the export keeps canyon-companion's run format", () => {
  const doc = c.exportDocument({ id: "r" }, [{ t: 2 }, { t: 1 }], [{ t: 9 }, { t: 3 }]);
  assert.equal(doc.format, "canyon-companion-run");
  assert.equal(doc.version, 1);
  assert.deepEqual(doc.events.map((e) => e.t), [1, 2]);
  assert.deepEqual(doc.fixes.map((f) => f.t), [3, 9]);
});

test("formatters", () => {
  assert.equal(c.formatDistance(-42.4), "42 m");
  assert.equal(c.formatDistance(1234), "1.23 km");
  assert.equal(c.formatHeight(10), "10 m");
  assert.equal(c.formatDuration(3_725_000), "1:02:05");
  assert.equal(c.formatDuration(-5), "0:00:00");
  assert.equal(c.formatAge(12_000), "12 s ago");
  assert.equal(c.formatAge(150_000), "3 min ago");
});

test("imperial formatters", () => {
  assert.equal(c.formatDistance(100, "imperial"), "328 ft");
  assert.equal(c.formatDistance(-100, "imperial"), "328 ft");
  assert.equal(c.formatDistance(304, "imperial"), "997 ft");
  assert.equal(c.formatDistance(305, "imperial"), "0.19 mi");
  assert.equal(c.formatDistance(1609.344, "imperial"), "1.00 mi");
  assert.equal(c.formatHeight(10, "imperial"), "33 ft");
  assert.equal(c.formatHeight(10.7), "11 m");
  assert.equal(c.formatKilometres(4.9656), "5.0 km");
  assert.equal(c.formatKilometres(4.9656, "imperial"), "3.1 mi");
});

test("heading from orientation: flat device reports where its top points", () => {
  assert.equal(Math.round(c.headingFromOrientation(0, 0, 0)), 0);
  assert.equal(Math.round(c.headingFromOrientation(90, 0, 0)), 270); // rotated CCW: top points west
  assert.equal(Math.round(c.headingFromOrientation(270, 0, 0)), 90);
  assert.equal(Math.round(c.headingFromOrientation(180, 5, 5)), 180);
});

test("heading from orientation: upright device reports where its back points", () => {
  assert.equal(Math.round(c.headingFromOrientation(0, 90, 0)), 0);
  assert.equal(Math.round(c.headingFromOrientation(90, 90, 0)), 270);
  assert.equal(Math.round(c.headingFromOrientation(180, 90, 0)), 180);
  assert.equal(Math.round(c.headingFromOrientation(300, 80, 0)), 60);
});

test("heading from orientation: tilted in hand stays continuous", () => {
  // Phone at 45 degrees facing east (alpha 270): both views of "forward" agree.
  assert.equal(Math.round(c.headingFromOrientation(270, 45, 0)), 90);
  assert.equal(c.headingFromOrientation(null, 0, 0), null);
});

test("screen rotation and cardinals", () => {
  assert.equal(c.screenHeading(350, 90), 80);
  assert.equal(c.screenHeading(null, 90), null);
  assert.equal(c.screenHeading(10, 0), 10);
  assert.equal(c.cardinal(0), "N");
  assert.equal(c.cardinal(44), "NE");
  assert.equal(c.cardinal(359), "N");
  assert.equal(c.cardinal(225), "SW");
});

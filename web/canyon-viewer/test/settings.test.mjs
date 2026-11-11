// Run with: npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const lib = require("../settings.js");
const memory = (initial) => {
  const data = new Map(initial ? [[lib.KEY, initial]] : []);
  return { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), data };
};

const base = { units: "metric", compass: false, hotC: lib.HOT_C, coldC: lib.COLD_C };

test("defaults when nothing is stored or the stored value is junk", () => {
  assert.deepEqual(lib.create(memory()).get(), base);
  assert.deepEqual(lib.create(memory("not json")).get(), base);
  assert.deepEqual(lib.create(memory('{"units":"furlongs","compass":"yes"}')).get(), base);
});

test("hot and cold default to 85 and 60 degrees F", () => {
  assert.equal(Math.round(lib.DEFAULTS.hotC * 1.8 + 32), 85);
  assert.equal(Math.round(lib.DEFAULTS.coldC * 1.8 + 32), 60);
});

test("hot and cold limits are validated, and must not cross", () => {
  const settings = lib.create(memory());
  settings.set({ hotC: 35, coldC: 10 });
  assert.deepEqual([settings.get().hotC, settings.get().coldC], [35, 10]);
  settings.set({ hotC: "warm" }); // invalid values fall back to the default, like units
  assert.deepEqual([settings.get().hotC, settings.get().coldC], [lib.HOT_C, 10]);
  settings.set({ coldC: 99 });
  assert.equal(settings.get().coldC, lib.COLD_C);
  settings.set({ hotC: 35, coldC: 40 }); // cold above hot: both back to the defaults
  assert.deepEqual([settings.get().hotC, settings.get().coldC], [lib.HOT_C, lib.COLD_C]);
});

test("set validates, persists and notifies", () => {
  const storage = memory();
  const settings = lib.create(storage);
  const seen = [];
  settings.onChange((s) => seen.push(s));
  settings.set({ units: "imperial" });
  settings.set({ compass: true, units: "bogus" });
  assert.deepEqual(settings.get(), { ...base, compass: true });
  assert.equal(seen.length, 2);
  assert.deepEqual(JSON.parse(storage.data.get(lib.KEY)), { ...base, compass: true });
  assert.deepEqual(lib.create(storage).get(), { ...base, compass: true });
});

test("blocked storage does not break settings", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const settings = lib.create(broken);
  settings.set({ units: "imperial" });
  assert.equal(settings.get().units, "imperial");
});

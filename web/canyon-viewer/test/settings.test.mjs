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

test("defaults when nothing is stored or the stored value is junk", () => {
  assert.deepEqual(lib.create(memory()).get(), { units: "metric", compass: false });
  assert.deepEqual(lib.create(memory("not json")).get(), { units: "metric", compass: false });
  assert.deepEqual(lib.create(memory('{"units":"furlongs","compass":"yes"}')).get(),
    { units: "metric", compass: false });
});

test("set validates, persists and notifies", () => {
  const storage = memory();
  const settings = lib.create(storage);
  const seen = [];
  settings.onChange((s) => seen.push(s));
  settings.set({ units: "imperial" });
  settings.set({ compass: true, units: "bogus" });
  assert.deepEqual(settings.get(), { units: "metric", compass: true });
  assert.equal(seen.length, 2);
  assert.deepEqual(JSON.parse(storage.data.get(lib.KEY)), { units: "metric", compass: true });
  assert.deepEqual(lib.create(storage).get(), { units: "metric", compass: true });
});

test("blocked storage does not break settings", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const settings = lib.create(broken);
  settings.set({ units: "imperial" });
  assert.equal(settings.get().units, "imperial");
});

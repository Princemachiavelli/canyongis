import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { makeIdb } from "./fake-idb.mjs";

const { openFileCache } = createRequire(import.meta.url)("../filecache.js");
const bytes = (n, fill = 1) => new Uint8Array(n).fill(fill);

test("keeps several files, newest first, and reopens the last saved", async () => {
  const cache = await openFileCache(makeIdb());
  assert.deepEqual(await cache.list(), []);
  assert.equal(await cache.info(), null);
  assert.equal(await cache.load(), null);
  await cache.save("a.gpkg", bytes(10, 1));
  await new Promise((r) => setTimeout(r, 5));
  await cache.save("b.gpkg", bytes(20, 2), { label: "Lytle Creek" });
  const files = await cache.list();
  assert.deepEqual(files.map((f) => f.name), ["b.gpkg", "a.gpkg"]);
  assert.deepEqual(files.map((f) => f.size), [20, 10]);
  assert.equal(files[0].label, "Lytle Creek");
  assert.ok(!("id" in files[0]) && !("bytes" in files[0]));
  assert.equal((await cache.info()).name, "b.gpkg");
  assert.deepEqual(Array.from((await cache.load()).bytes), Array.from(bytes(20, 2)));
  assert.deepEqual(Array.from((await cache.load("a.gpkg")).bytes), Array.from(bytes(10, 1)));
  await cache.setLast("a.gpkg");
  assert.equal((await cache.info()).name, "a.gpkg");
  assert.equal(await cache.load("missing.gpkg"), null);
});

test("saving a name again replaces that file only", async () => {
  const cache = await openFileCache(makeIdb());
  await cache.save("a.gpkg", bytes(10));
  await cache.save("b.gpkg", bytes(5));
  await cache.save("a.gpkg", bytes(30, 9));
  assert.deepEqual((await cache.list()).map((f) => [f.name, f.size]).sort(), [["a.gpkg", 30], ["b.gpkg", 5]]);
});

test("listing does not read any file's bytes", async () => {
  const idb = makeIdb();
  const cache = await openFileCache(idb);
  await cache.save("a.gpkg", bytes(10));
  await cache.save("b.gpkg", bytes(10));
  idb.reads.length = 0;
  await cache.list();
  await cache.info();
  assert.ok(idb.reads.every((k) => !String(k).startsWith("bytes:")), idb.reads.join());
});

test("removing the last opened file points back at the newest remaining", async () => {
  const cache = await openFileCache(makeIdb());
  await cache.save("a.gpkg", bytes(1));
  await new Promise((r) => setTimeout(r, 5));
  await cache.save("b.gpkg", bytes(1));
  await cache.remove("b.gpkg");
  assert.deepEqual((await cache.list()).map((f) => f.name), ["a.gpkg"]);
  assert.equal((await cache.info()).name, "a.gpkg");
  await cache.remove(); // default: the last opened
  assert.deepEqual(await cache.list(), []);
  assert.equal(await cache.info(), null);
  await cache.remove(); // nothing left: no error
});

test("removing another file keeps the last-opened pointer", async () => {
  const cache = await openFileCache(makeIdb());
  await cache.save("a.gpkg", bytes(1));
  await cache.save("b.gpkg", bytes(1));
  await cache.remove("a.gpkg");
  assert.equal((await cache.info()).name, "b.gpkg");
});

test("the single file saved by the previous version is moved, not lost", async () => {
  const old = bytes(40, 7);
  const idb = makeIdb({
    "last:meta": { id: "last:meta", name: "canyons.gpkg", size: 40, savedAt: 1234 },
    "last:bytes": { id: "last:bytes", bytes: old },
  });
  const cache = await openFileCache(idb);
  const files = await cache.list();
  assert.deepEqual(files.map((f) => [f.name, f.size, f.savedAt]), [["canyons.gpkg", 40, 1234]]);
  assert.equal((await cache.info()).name, "canyons.gpkg");
  assert.deepEqual(Array.from((await cache.load()).bytes), Array.from(old));
  assert.ok(!idb.stores.files.has("last:meta") && !idb.stores.files.has("last:bytes"));
  // a second open changes nothing
  const again = await openFileCache(idb);
  assert.equal((await again.list()).length, 1);
});

test("a write the browser refuses (no space) leaves the earlier files untouched", async () => {
  const idb = makeIdb();
  const cache = await openFileCache(idb);
  await cache.save("keep.gpkg", bytes(10, 3));
  idb.failWrites = true;
  await assert.rejects(cache.save("big.gpkg", bytes(99)), /Quota/);
  idb.failWrites = false;
  assert.deepEqual((await cache.list()).map((f) => f.name), ["keep.gpkg"]);
  assert.equal((await cache.info()).name, "keep.gpkg");
  assert.deepEqual(Array.from((await cache.load()).bytes), Array.from(bytes(10, 3)));
});

test("a saved file's canyon summary is kept, and can be added later", async () => {
  const cache = await openFileCache(makeIdb());
  const canyons = [{ title: "Lytle Creek (Middle Fork)", region: "Lytle Creek Area" }];
  await cache.save("with.gpkg", bytes(5), { label: "Lytle", canyons });
  await cache.save("without.gpkg", bytes(7));
  const byName = Object.fromEntries((await cache.list()).map((f) => [f.name, f]));
  assert.deepEqual(byName["with.gpkg"].canyons, canyons);
  assert.equal(byName["without.gpkg"].canyons, null);
  assert.equal(await cache.setMeta("without.gpkg", { canyons }), true);
  const after = Object.fromEntries((await cache.list()).map((f) => [f.name, f]));
  assert.deepEqual(after["without.gpkg"].canyons, canyons);
  assert.equal(after["without.gpkg"].size, 7); // the rest of the record is untouched
  assert.equal(await cache.setMeta("missing.gpkg", { canyons }), false);
  assert.ok(!(await cache.list()).some((f) => f.name === "missing.gpkg"));
});

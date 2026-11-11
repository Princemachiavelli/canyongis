import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const { create, CatalogError, isSqlite } = createRequire(import.meta.url)("../catalog.js");
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json" } });
const gpkgBytes = (n = 64) => {
  const out = new Uint8Array(n);
  out.set([...("SQLite format 3\0")].map((c) => c.charCodeAt(0)));
  return out;
};
const fakeFetch = (handler) => {
  const calls = [];
  const fn = async (url, options = {}) => { calls.push({ url, ...options }); return handler(url, options); };
  fn.calls = calls;
  return fn;
};

test("status is the server's answer, or null when anything is wrong", async () => {
  assert.deepEqual(await create({ fetchImpl: fakeFetch(() => json({ ok: true, tiles: true })) }).status(),
    { ok: true, tiles: true });
  assert.equal(await create({ fetchImpl: fakeFetch(() => json({ error: "not enabled" }, 404)) }).status(), null);
  assert.equal(await create({ fetchImpl: fakeFetch(() => { throw new TypeError("Failed to fetch"); }) }).status(), null);
  assert.equal(await create({ fetchImpl: fakeFetch(() => new Response("<html>", { status: 200 })) }).status(), null);
  assert.equal(await create({ fetchImpl: fakeFetch(() => json({ ok: false })) }).status(), null);
});

test("status gives up after a few seconds", async () => {
  const hang = (url, { signal }) => new Promise((_, reject) =>
    signal.addEventListener("abort", () => reject(new Error("aborted"))));
  const started = Date.now();
  const result = await create({ fetchImpl: hang }).status();
  assert.equal(result, null);
  assert.ok(Date.now() - started < 6000);
});

test("search encodes the text and returns the results; errors carry the server's message", async () => {
  const f = fakeFetch(() => json({ results: [{ title: "Lytle Creek (Middle Fork)" }] }));
  const results = await create({ fetchImpl: f }).search("lytle & ]] mid");
  assert.equal(results[0].title, "Lytle Creek (Middle Fork)");
  assert.equal(f.calls[0].url, "api/search?q=lytle%20%26%20%5D%5D%20mid");
  const bad = create({ fetchImpl: fakeFetch(() => json({ error: "Ropewiki search failed: down" }, 502)) });
  await assert.rejects(bad.search("x"), (err) => err instanceof CatalogError && err.status === 502 && /down/.test(err.message));
  const html = create({ fetchImpl: fakeFetch(() => new Response("oops", { status: 500 })) });
  await assert.rejects(html.search("x"), /HTTP 500/);
});

test("startBuild posts JSON and returns the job", async () => {
  const f = fakeFetch(() => json({ job: { id: "a".repeat(32), state: "queued" } }, 202));
  const job = await create({ fetchImpl: f, base: "/app/" }).startBuild("Lytle Creek (Middle Fork)", { tiles: false });
  assert.equal(job.state, "queued");
  assert.equal(f.calls[0].url, "/app/api/builds");
  assert.equal(f.calls[0].method, "POST");
  assert.equal(f.calls[0].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(f.calls[0].body), { title: "Lytle Creek (Middle Fork)", tiles: false });
  await assert.rejects(create({ fetchImpl: fakeFetch(() => json({ error: "x is not a canyon page" }, 400)) }).startBuild("x"), /not a canyon/);
});

test("waitForJob follows the job to the end and reports each state", async () => {
  const states = ["queued", "running", "running", "done"];
  let i = 0;
  const f = fakeFetch(() => json({ job: { id: "j", state: states[i++], stage: `s${i}` } }));
  const seen = [];
  const job = await create({ fetchImpl: f }).waitForJob("j", { onUpdate: (j) => seen.push(j.stage), wait: async () => {} });
  assert.equal(job.state, "done");
  assert.deepEqual(seen, ["s1", "s2", "s3", "s4"]);
  const failed = fakeFetch(() => json({ job: { id: "j", state: "failed", error: "nope" } }));
  assert.equal((await create({ fetchImpl: failed }).waitForJob("j", { wait: async () => {} })).error, "nope");
});

test("waitForJob rides out brief failures, gives up on long ones, and reports a forgotten job", async () => {
  let n = 0;
  const flaky = fakeFetch(() => {
    n += 1;
    if (n <= 3) throw new TypeError("Failed to fetch");
    return json({ job: { id: "j", state: "done" } });
  });
  assert.equal((await create({ fetchImpl: flaky }).waitForJob("j", { wait: async () => {} })).state, "done");
  const down = fakeFetch(() => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(create({ fetchImpl: down }).waitForJob("j", { wait: async () => {}, maxFailures: 3 }), /Lost the connection/);
  assert.equal(down.calls.length, 3);
  const gone = fakeFetch(() => json({ error: "Unknown job" }, 404));
  await assert.rejects(create({ fetchImpl: gone }).waitForJob("j", { wait: async () => {} }),
    (err) => err.status === 404);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(create({ fetchImpl: down }).waitForJob("j", { signal: controller.signal }), /Cancelled/);
});

test("download streams with progress and checks the file", async () => {
  const data = gpkgBytes(100);
  const f = fakeFetch(() => new Response(data, { headers: { "content-length": "100" } }));
  const progress = [];
  const got = await create({ fetchImpl: f }).download("builds/x.gpkg", { onProgress: (a, b) => progress.push([a, b]) });
  assert.deepEqual(Array.from(got), Array.from(data));
  assert.ok(progress.length >= 1 && progress.at(-1)[0] === 100 && progress.at(-1)[1] === 100);
});

test("download rejects short, non-GeoPackage and failed responses", async () => {
  const short = fakeFetch(() => new Response(gpkgBytes(50), { headers: { "content-length": "100" } }));
  await assert.rejects(create({ fetchImpl: short }).download("x"), /cut short/);
  const html = fakeFetch(() => new Response("<html>not a db</html>"));
  await assert.rejects(create({ fetchImpl: html }).download("x"), /not a GeoPackage/);
  await assert.rejects(create({ fetchImpl: fakeFetch(() => new Response("", { status: 404 })) }).download("x"), /HTTP 404/);
  assert.ok(isSqlite(gpkgBytes()) && !isSqlite(new Uint8Array(3)));
});

test("manifest, startUpdate and getDelta talk to the update endpoints", async () => {
  const f = fakeFetch((url) => {
    if (url.startsWith("api/manifest")) return json({ manifest: { title: "X", sections: {} } });
    if (url === "api/updates") return json({ job: { id: "j".repeat(32), kind: "update" } }, 202);
    return json({ delta: { title: "X", sections: {} } });
  });
  const c = create({ fetchImpl: f });
  assert.equal((await c.manifest("Lytle Creek (Middle Fork)")).title, "X");
  assert.equal(f.calls[0].url, "api/manifest?title=Lytle%20Creek%20(Middle%20Fork)");
  const job = await c.startUpdate("X", { beta: "abc" }, { force: true });
  assert.equal(job.kind, "update");
  assert.deepEqual(JSON.parse(f.calls[1].body), { title: "X", have: { beta: "abc" }, force: true });
  assert.equal(f.calls[1].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse((await (async () => { await c.startUpdate("X", {}); return f.calls[2]; })()).body).force, false);
  assert.equal((await c.getDelta("j".repeat(32))).title, "X");
  assert.equal(f.calls[3].url, `api/updates/${"j".repeat(32)}/delta`);
  const missing = create({ fetchImpl: fakeFetch(() => json({ error: "This server does not have that canyon yet." }, 404)) });
  await assert.rejects(missing.manifest("Nope"), (err) => err.status === 404 && /does not have/.test(err.message));
});

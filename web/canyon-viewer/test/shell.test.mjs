import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = (name) => fileURLToPath(new URL(`../${name}`, import.meta.url));
const html = readFileSync(here("index.html"), "utf8");
const sw = readFileSync(here("sw.js"), "utf8");
const manifest = JSON.parse(readFileSync(here("manifest.webmanifest"), "utf8"));

const local = (url) => !/^(https?:)?\/\//.test(url);
const shell = [...sw.slice(sw.indexOf("const SHELL = ["), sw.indexOf("const CDN = [")).matchAll(/"([^"]+)"/g)]
  .map((m) => m[1]);

test("every local script, stylesheet and icon the page loads is cached by the service worker", () => {
  const used = [
    ...[...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]),
    ...[...html.matchAll(/<link[^>]+href="([^"]+)"/g)].map((m) => m[1]),
  ].filter(local);
  assert.ok(used.length > 10);
  for (const file of used) assert.ok(shell.includes(file), `${file} is missing from SHELL in sw.js`);
  for (const file of shell.filter((f) => f !== "./")) {
    assert.ok(existsSync(here(file)), `${file} is listed in SHELL but does not exist`);
  }
});

test("the manifest is installable and its icons exist", () => {
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.start_url, "./");
  const sizes = manifest.icons.map((i) => i.sizes);
  assert.ok(sizes.includes("192x192") && sizes.includes("512x512"));
  for (const icon of manifest.icons) assert.ok(existsSync(here(icon.src)), icon.src);
  assert.ok(existsSync(here("icons/apple-touch-icon.png")));
  assert.match(html, /rel="apple-touch-icon"/);
  assert.match(html, /name="apple-mobile-web-app-capable"/);
});

test("the installed app is placed below the status bar with room to spare", () => {
  // viewport-fit=cover makes env(safe-area-inset-top) report the status bar; the installed app
  // (and only it) starts below that, plus extra room for iOS's fade over the top of the page.
  const viewport = /<meta name="viewport" content="([^"]*)"/.exec(html)[1];
  assert.ok(/viewport-fit=cover/.test(viewport), viewport);
  const css = readFileSync(here("style.css"), "utf8");
  assert.match(css, /\.is-standalone body \{ padding-top: calc\(env\(safe-area-inset-top\) \+ 24px\); \}/);
  assert.ok(!/^body \{[^}]*safe-area-inset-top/m.test(css), "browser tabs are not padded");
  assert.match(readFileSync(here("app.js"), "utf8"), /classList\.add\("is-standalone"\)/);
});

test("links get a readable colour in every theme", () => {
  const css = readFileSync(here("style.css"), "utf8");
  // light, dark by system setting, dark chosen explicitly
  assert.equal((css.match(/--link:/g) || []).length, 3);
  assert.match(css, /a, a:visited \{ color: var\(--link\); \}/);
});

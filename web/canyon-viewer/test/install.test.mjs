import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const { installState } = createRequire(import.meta.url)("../install.js");
const SAFARI_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
  + "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const CHROME_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
  + "(KHTML, like Gecko) CriOS/120.0 Mobile/15E148 Safari/604.1";
const IPAD = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
  + "(KHTML, like Gecko) Version/17.0 Safari/605.1.15";

test("installed apps are recognised either way the browser reports it", () => {
  assert.equal(installState({ standalone: true }).mode, "installed");
  assert.equal(installState({ displayStandalone: true }).mode, "installed");
});

test("a kept install prompt wins over the platform hint", () => {
  assert.equal(installState({ hasPrompt: true, userAgent: SAFARI_IOS }).mode, "prompt");
});

test("iOS Safari and iPadOS get the Share menu steps; other iOS browsers are sent to Safari", () => {
  assert.match(installState({ userAgent: SAFARI_IOS }).text, /Add to Home Screen/);
  assert.equal(installState({ userAgent: IPAD, maxTouchPoints: 5 }).mode, "ios");
  assert.match(installState({ userAgent: CHROME_IOS }).text, /open this page in Safari/);
  assert.equal(installState({ userAgent: IPAD, maxTouchPoints: 0 }).mode, "other");
});

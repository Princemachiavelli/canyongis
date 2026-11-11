import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const f = require("../forecast.js");
const payload = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../../tests/fixtures/open-meteo.json", import.meta.url)), "utf8"));

test("the request asks for the daily variables the table stores", () => {
  const url = new URL(f.forecastUrl(34.2533, -117.57694));
  assert.equal(url.origin + url.pathname, f.API_URL);
  assert.equal(url.searchParams.get("latitude"), "34.2533");
  assert.equal(url.searchParams.get("longitude"), "-117.5769");
  assert.equal(url.searchParams.get("timezone"), "auto");
  assert.deepEqual(url.searchParams.get("daily").split(","), Object.keys(f.DAILY_VARIABLES));
});

test("parses the response into forecast_days columns (as the Python parser does)", () => {
  const parsed = f.parseForecast(payload, "2026-11-09T12:00:00Z");
  assert.equal(parsed.days.length, 3);
  assert.deepEqual(parsed.days[2], {
    date: "2026-11-11", weather_code: 95, temp_max_c: 12.0, temp_min_c: 5.9, precip_mm: 21.7,
    precip_prob_pct: 90, wind_max_kmh: 31.5, gust_max_kmh: 62.0,
    sunrise: "2026-11-11T06:27", sunset: "2026-11-11T16:49",
  });
  assert.equal(parsed.elevation_m, 1021);
  assert.throws(() => f.parseForecast({ daily: {} }));
});

test("fetchForecast uses the injected fetch and reports failures", async () => {
  const seen = [];
  const ok = async (url) => { seen.push(url); return { ok: true, json: async () => payload }; };
  const got = await f.fetchForecast(34.25, -117.5625, ok);
  assert.equal(got.days.length, 3);
  assert.match(seen[0], /latitude=34\.2500/);
  assert.match(got.fetched_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  await assert.rejects(f.fetchForecast(1, 2, async () => ({ ok: false, status: 503 })), /503/);
  await assert.rejects(f.fetchForecast(1, 2, async () => { throw new TypeError("Failed to fetch"); }), /Failed to fetch/);
  await assert.rejects(f.fetchForecast(1, 2, async () => ({ ok: true, json: async () => ({ error: true, reason: "bad" }) })), /bad/);
});

test("newest picks the later fetch", () => {
  const a = { fetched_at: "2026-11-09T12:00:00Z" };
  const b = { fetched_at: "2026-11-10T12:00:00Z" };
  assert.equal(f.newest(a, b), b);
  assert.equal(f.newest(b, a), b);
  assert.equal(f.newest(null, a), a);
  assert.equal(f.newest(null, null), null);
});

test("formats weather and units", () => {
  assert.equal(f.describeWeather(95), "Thunderstorm");
  assert.equal(f.describeWeather(1234), "Unknown");
  assert.ok(f.isThunderstorm(96) && !f.isThunderstorm(61));
  assert.equal(f.formatTemp(10, "imperial"), "50°F");
  assert.equal(f.formatTemp(10.4), "10°C");
  assert.equal(f.formatRain(25.4, "imperial"), "1.00 in");
  assert.equal(f.formatRain(3.44), "3.4 mm");
  assert.equal(f.formatRain(21.7), "22 mm");
  assert.equal(f.formatWind(16.09344, "imperial"), "10 mph");
  assert.equal(f.clockTime("2026-11-09T06:25"), "06:25");
  assert.equal(f.formatTemp(null), "–");
});

const icons = require("../weather-icons.js");

test("every weather code maps to a picture that exists", () => {
  const expected = { 0: "sunny", 1: "sunny", 2: "partly", 3: "cloudy", 45: "fog", 48: "fog",
    51: "rain", 55: "rain", 61: "rain", 65: "rain", 67: "rain", 80: "rain", 82: "rain",
    71: "snow", 75: "snow", 77: "snow", 85: "snow", 95: "storm", 96: "storm", 99: "storm" };
  for (const [code, name] of Object.entries(expected)) {
    assert.equal(f.weatherCategory(Number(code)), name, `code ${code}`);
  }
  assert.equal(f.weatherCategory(null), null);
  assert.equal(f.weatherCategory(1234), null);
  for (const name of Object.values(expected)) assert.ok(icons.NAMES.includes(name), name);
  for (const name of icons.NAMES) assert.match(icons.svg(name), /^<svg [^>]*>.*<\/svg>$/s);
  assert.throws(() => icons.svg("hail-of-frogs"));
});

test("today is found in the forecast's time zone, not the device's", () => {
  const record = f.parseForecast(payload, "2026-11-09T12:00:00Z"); // America/Los_Angeles
  const lateEveningLA = Date.UTC(2026, 10, 10, 3, 0); // Nov 9, 19:00 in Los Angeles; Nov 10 in UTC
  assert.equal(f.todayAt("America/Los_Angeles", lateEveningLA), "2026-11-09");
  assert.equal(f.dayForToday(record, lateEveningLA).date, "2026-11-09");
  assert.equal(f.dayForToday(record, Date.UTC(2026, 10, 11, 20, 0)).date, "2026-11-11");
  assert.equal(f.dayForToday(record, Date.UTC(2026, 10, 20, 20, 0)), null); // forecast too old
  assert.equal(f.dayForToday(null), null);
  assert.equal(f.todayAt("Not/AZone", lateEveningLA).length, 10); // falls back to the device zone
});

test("hot and cold use the day's high against the limits", () => {
  const hot = 29.4444;
  const cold = 15.5556;
  assert.equal(f.temperatureFlag({ temp_max_c: 30 }, hot, cold), "hot");
  assert.equal(f.temperatureFlag({ temp_max_c: hot }, hot, cold), null); // above, not at
  assert.equal(f.temperatureFlag({ temp_max_c: 20 }, hot, cold), null);
  assert.equal(f.temperatureFlag({ temp_max_c: 10 }, hot, cold), "cold");
  assert.equal(f.temperatureFlag({ temp_max_c: null }, hot, cold), null);
  assert.equal(f.temperatureFlag(null, hot, cold), null);
});

test("iconsFor gives the weather picture, then hot or cold", () => {
  const limits = { hotC: 29.4444, coldC: 15.5556 };
  const mild = { weather_code: 2, temp_max_c: 22 };
  assert.deepEqual(f.iconsFor(mild, limits), [{ name: "partly", label: "Partly cloudy" }]);
  const scorcher = { weather_code: 0, temp_max_c: 35 };
  assert.deepEqual(f.iconsFor(scorcher, limits, "imperial"), [
    { name: "sunny", label: "Clear" }, { name: "hot", label: "Hot: high of 95°F" }]);
  assert.deepEqual(f.iconsFor({ weather_code: 61, temp_max_c: 5 }, limits).map((i) => i.name),
    ["rain", "cold"]);
  assert.deepEqual(f.iconsFor({ weather_code: 999, temp_max_c: 20 }, limits), []);
  assert.deepEqual(f.iconsFor(null, limits), []);
});

test("hourly values are parsed in the table's columns", () => {
  const record = f.parseForecast(payload, "2026-11-09T12:00:00Z");
  assert.equal(record.hours.length, 72);
  assert.deepEqual(Object.keys(record.hours[0]), f.HOUR_COLUMNS);
  const stormy = record.hours.find((h) => h.time === "2026-11-11T14:00");
  assert.equal(stormy.weather_code, 95);
  assert.equal(stormy.precip_prob_pct, 80);
  assert.match(f.forecastUrl(34.25, -117.5), /hourly=weather_code%2Ctemperature_2m/);
  assert.deepEqual(f.parseForecast({ ...payload, hourly: undefined }).hours, []);
  assert.match(f.hourLabel("2026-11-10T14:00"), /2|14/);
});

test("the hours shown run from 2 hours ago to 24 hours ahead, into tomorrow", () => {
  const record = f.parseForecast(payload, "2026-11-09T12:00:00Z");
  const noon = Date.UTC(2026, 10, 10, 20, 0); // 12:00 on Nov 10 in Los Angeles
  assert.equal(f.currentHourAt("America/Los_Angeles", noon), "2026-11-10T12:00");
  assert.equal(f.currentHourAt("UTC", noon), "2026-11-10T20:00");
  assert.equal(f.currentHourAt("Not/AZone", noon).length, 16);
  const window = f.hoursAround(record, noon);
  assert.equal(window.length, 27);
  assert.equal(window[0].time, "2026-11-10T10:00"); // two hours ago
  assert.equal(window[2].time, "2026-11-10T12:00"); // now
  assert.equal(window.at(-1).time, "2026-11-11T12:00"); // 24 hours ahead, tomorrow
  assert.ok(window.some((h) => h.time.startsWith("2026-11-11T")));
  assert.equal(f.hoursAround(record, noon, 0, 3).length, 4);
  // near the start of the data there are fewer hours before; near the end, fewer after
  assert.equal(f.hoursAround(record, Date.UTC(2026, 10, 9, 9, 0))[0].time, "2026-11-09T00:00");
  assert.equal(f.hoursAround(record, Date.UTC(2026, 10, 12, 4, 0)).at(-1).time, "2026-11-11T23:00");
  // a forecast that no longer reaches the present, one with no hours, and no forecast
  assert.deepEqual(f.hoursAround(record, Date.UTC(2026, 11, 20)), []);
  assert.deepEqual(f.hoursAround({ ...record, hours: undefined }, noon), []);
  assert.deepEqual(f.hoursAround(null), []);
});

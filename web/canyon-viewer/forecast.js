// Weather forecast: the Open-Meteo request, its mapping to the GeoPackage's forecast columns
// (the same as src/canyongis/geopackage/forecast.py, both tested against
// tests/fixtures/open-meteo.json), and display helpers. No DOM, so it runs in the browser
// (global `forecast`) and in Node for tests. Open-Meteo's free endpoint is for non-commercial
// use, needs no key and allows cross-origin requests.
(function (root) {
  "use strict";

  const SOURCE = "open-meteo";
  const API_URL = "https://api.open-meteo.com/v1/forecast";
  const FORECAST_DAYS = 7;
  // Open-Meteo variable -> forecast_days column (API default units: degC, mm, km/h).
  const DAILY_VARIABLES = {
    weather_code: "weather_code",
    temperature_2m_max: "temp_max_c",
    temperature_2m_min: "temp_min_c",
    precipitation_sum: "precip_mm",
    precipitation_probability_max: "precip_prob_pct",
    wind_speed_10m_max: "wind_max_kmh",
    wind_gusts_10m_max: "gust_max_kmh",
    sunrise: "sunrise",
    sunset: "sunset",
  };
  const COLUMNS = ["date", ...Object.values(DAILY_VARIABLES)];
  // Hourly values: Open-Meteo variable -> forecast_hours column.
  const HOURLY_VARIABLES = {
    weather_code: "weather_code",
    temperature_2m: "temp_c",
    precipitation: "precip_mm",
    precipitation_probability: "precip_prob_pct",
    wind_speed_10m: "wind_kmh",
    wind_gusts_10m: "gust_kmh",
  };
  const HOUR_COLUMNS = ["time", ...Object.values(HOURLY_VARIABLES)];

  function forecastUrl(lat, lon) {
    const query = new URLSearchParams({
      latitude: lat.toFixed(4),
      longitude: lon.toFixed(4),
      daily: Object.keys(DAILY_VARIABLES).join(","),
      hourly: Object.keys(HOURLY_VARIABLES).join(","),
      timezone: "auto",
      forecast_days: String(FORECAST_DAYS),
      wind_speed_unit: "kmh",
      temperature_unit: "celsius",
      precipitation_unit: "mm",
    });
    return `${API_URL}?${query}`;
  }

  // ISO 8601 UTC with whole seconds, so stored times compare correctly as text.
  function isoNow(now = Date.now()) {
    return new Date(now).toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function parseForecast(payload, fetchedAt = isoNow()) {
    const daily = (payload && payload.daily) || {};
    const dates = daily.time || [];
    if (!dates.length) throw new Error("The forecast response has no daily data");
    const days = dates.map((date, i) => {
      const day = { date };
      for (const [variable, column] of Object.entries(DAILY_VARIABLES)) {
        const values = daily[variable] || [];
        day[column] = i < values.length ? values[i] : null;
      }
      return day;
    });
    const hourly = payload.hourly || {};
    const hours = (hourly.time || []).map((time, i) => {
      const hour = { time };
      for (const [variable, column] of Object.entries(HOURLY_VARIABLES)) {
        const values = hourly[variable] || [];
        hour[column] = i < values.length ? values[i] : null;
      }
      return hour;
    });
    return {
      source: SOURCE,
      fetched_at: fetchedAt,
      latitude: payload.latitude,
      longitude: payload.longitude,
      elevation_m: payload.elevation ?? null,
      timezone: payload.timezone ?? null,
      days,
      hours,
    };
  }

  // `fetchImpl` is injectable for tests. Rejects when offline or the service errors.
  async function fetchForecast(lat, lon, fetchImpl = root.fetch.bind(root)) {
    const response = await fetchImpl(forecastUrl(lat, lon));
    if (!response.ok) throw new Error(`Forecast service answered HTTP ${response.status}`);
    const payload = await response.json();
    if (payload.error) throw new Error(payload.reason || "The forecast service returned an error");
    return parseForecast(payload);
  }

  // The forecast with the later fetch time (either may be null).
  function newest(a, b) {
    if (!a) return b || null;
    if (!b) return a;
    return b.fetched_at > a.fetched_at ? b : a;
  }

  // ---- display ---------------------------------------------------------------------

  // WMO weather interpretation codes, as Open-Meteo reports them.
  const WEATHER = [
    [[0], "Clear"], [[1], "Mostly clear"], [[2], "Partly cloudy"], [[3], "Overcast"],
    [[45, 48], "Fog"], [[51, 53, 55], "Drizzle"], [[56, 57], "Freezing drizzle"],
    [[61], "Light rain"], [[63], "Rain"], [[65], "Heavy rain"], [[66, 67], "Freezing rain"],
    [[71, 73, 75, 77], "Snow"], [[80], "Light showers"], [[81], "Showers"],
    [[82], "Violent showers"], [[85, 86], "Snow showers"],
    [[95], "Thunderstorm"], [[96, 99], "Thunderstorm with hail"],
  ];

  function describeWeather(code) {
    const hit = WEATHER.find(([codes]) => codes.includes(code));
    return hit ? hit[1] : "Unknown";
  }

  const isThunderstorm = (code) => code === 95 || code === 96 || code === 99;

  // Which picture stands for a WMO weather code (see weather-icons.js), or null if unknown.
  function weatherCategory(code) {
    if (code === 0 || code === 1) return "sunny";
    if (code === 2) return "partly";
    if (code === 3) return "cloudy";
    if (code === 45 || code === 48) return "fog";
    if ([71, 73, 75, 77, 85, 86].includes(code)) return "snow";
    if (code >= 95 && code <= 99) return "storm";
    if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
    return null;
  }

  // Today's date (YYYY-MM-DD) at the forecast's location, so the right day is found whatever
  // the device's own time zone is.
  function todayAt(timezone, now = Date.now()) {
    const parts = (zone) => new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(new Date(now));
    let found;
    try { found = parts(timezone || undefined); } catch (err) { found = parts(undefined); }
    const get = (type) => found.find((p) => p.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  }

  // The hour (YYYY-MM-DDTHH:00) it is now at the forecast's location, to match hourly times.
  function currentHourAt(timezone, now = Date.now()) {
    const parts = (zone) => new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(now));
    let found;
    try { found = parts(timezone || undefined); } catch (err) { found = parts(undefined); }
    const get = (type) => found.find((p) => p.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:00`;
  }

  // The hourly values from `before` hours ago to `after` hours ahead of now, continuing into
  // tomorrow if need be (empty for a forecast saved before hourly values existed, or one that no
  // longer reaches the present).
  function hoursAround(record, now = Date.now(), before = 2, after = 24) {
    if (!record || !record.hours || !record.hours.length) return [];
    const key = currentHourAt(record.timezone, now);
    const at = record.hours.findIndex((h) => h.time >= key);
    if (at < 0) return [];
    return record.hours.slice(Math.max(0, at - before), at + after + 1);
  }

  // "2 PM" or "14" in the device's style, from a local time such as "2026-11-09T14:00".
  function hourLabel(time) {
    const [date, clock] = time.split("T");
    const [y, m, d] = date.split("-").map(Number);
    return new Date(y, m - 1, d, Number(clock.slice(0, 2))).toLocaleTimeString([], { hour: "numeric" });
  }

  // The forecast day for today, or null when the forecast does not cover it (too old).
  function dayForToday(record, now = Date.now()) {
    if (!record) return null;
    const today = todayAt(record.timezone, now);
    return record.days.find((d) => d.date === today) || null;
  }

  // "hot" when the day's high is above hotC, "cold" when it is below coldC (the high is the
  // temperature of the part of the day spent in the canyon), else null.
  function temperatureFlag(day, hotC, coldC) {
    const high = day ? day.temp_max_c : null;
    if (high === null || high === undefined) return null;
    if (high > hotC) return "hot";
    if (high < coldC) return "cold";
    return null;
  }

  // The pictures for a day: [{name, label}] with the weather first, then hot / cold if it applies.
  function iconsFor(day, limits, units = "metric") {
    if (!day) return [];
    const out = [];
    const category = weatherCategory(day.weather_code);
    if (category) out.push({ name: category, label: describeWeather(day.weather_code) });
    const flag = temperatureFlag(day, limits.hotC, limits.coldC);
    if (flag) {
      out.push({
        name: flag,
        label: `${flag === "hot" ? "Hot" : "Cold"}: high of ${formatTemp(day.temp_max_c, units)}`,
      });
    }
    return out;
  }

  function formatTemp(c, units = "metric") {
    if (c === null || c === undefined) return "–";
    return units === "imperial" ? `${Math.round(c * 1.8 + 32)}°F` : `${Math.round(c)}°C`;
  }

  function formatRain(mm, units = "metric") {
    if (mm === null || mm === undefined) return "–";
    if (units === "imperial") return `${(mm / 25.4).toFixed(2)} in`;
    return `${mm < 10 ? mm.toFixed(1) : Math.round(mm)} mm`;
  }

  function formatWind(kmh, units = "metric") {
    if (kmh === null || kmh === undefined) return "–";
    return units === "imperial" ? `${Math.round(kmh / 1.609344)} mph` : `${Math.round(kmh)} km/h`;
  }

  // "06:25" from the local ISO time Open-Meteo gives ("2026-11-09T06:25").
  const clockTime = (iso) => (iso && iso.includes("T") ? iso.split("T")[1].slice(0, 5) : "–");

  const api = {
    SOURCE, API_URL, FORECAST_DAYS, DAILY_VARIABLES, COLUMNS, HOURLY_VARIABLES, HOUR_COLUMNS,
    currentHourAt, hoursAround, hourLabel,
    forecastUrl, isoNow, parseForecast, fetchForecast, newest,
    describeWeather, isThunderstorm, weatherCategory, todayAt, dayForToday, temperatureFlag,
    iconsFor, formatTemp, formatRain, formatWind, clockTime,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.forecast = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

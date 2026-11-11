// Viewer settings (units, compass, hot and cold limits), kept in localStorage. Works in the browser (global
// `settings`) and in Node for tests, where a storage object can be passed in.
(function (root) {
  "use strict";

  const KEY = "canyon-viewer.settings";
  // Hot and cold limits for the temperature pictures, always in degrees C (the defaults are
  // 85 and 60 degrees F). Shown and edited in the chosen units.
  const HOT_C = (85 - 32) / 1.8;
  const COLD_C = (60 - 32) / 1.8;
  const DEFAULTS = { units: "metric", compass: false, hotC: HOT_C, coldC: COLD_C };
  const validTemp = (v) => typeof v === "number" && Number.isFinite(v) && v >= -50 && v <= 60;

  // Keep only known keys with valid values, so a stale or hand-edited entry cannot break the page.
  function sanitize(raw) {
    const out = { ...DEFAULTS };
    if (raw && (raw.units === "metric" || raw.units === "imperial")) out.units = raw.units;
    if (raw && typeof raw.compass === "boolean") out.compass = raw.compass;
    if (raw && validTemp(raw.hotC)) out.hotC = raw.hotC;
    if (raw && validTemp(raw.coldC)) out.coldC = raw.coldC;
    // A cold limit at or above the hot one makes no sense: use the defaults for both.
    if (out.coldC >= out.hotC) {
      out.hotC = HOT_C;
      out.coldC = COLD_C;
    }
    return out;
  }

  function create(storage) {
    let current = { ...DEFAULTS };
    const listeners = new Set();
    try {
      current = sanitize(JSON.parse(storage.getItem(KEY)));
    } catch (err) { /* storage blocked or empty: defaults */ }

    return {
      get: () => ({ ...current }),
      set(patch) {
        current = sanitize({ ...current, ...patch });
        try { storage.setItem(KEY, JSON.stringify(current)); } catch (err) { /* ignore */ }
        for (const fn of listeners) fn({ ...current });
      },
      onChange(fn) { listeners.add(fn); },
    };
  }

  const api = { create, sanitize, DEFAULTS, KEY, HOT_C, COLD_C };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    let storage = null;
    try { storage = root.localStorage; } catch (err) { /* blocked */ }
    root.settings = create(storage || { getItem: () => null, setItem: () => {} });
    root.settingsLib = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

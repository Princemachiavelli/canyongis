// Viewer settings (units, compass), kept in localStorage. Works in the browser (global
// `settings`) and in Node for tests, where a storage object can be passed in.
(function (root) {
  "use strict";

  const KEY = "canyon-viewer.settings";
  const DEFAULTS = { units: "metric", compass: false };

  // Keep only known keys with valid values, so a stale or hand-edited entry cannot break the page.
  function sanitize(raw) {
    const out = { ...DEFAULTS };
    if (raw && (raw.units === "metric" || raw.units === "imperial")) out.units = raw.units;
    if (raw && typeof raw.compass === "boolean") out.compass = raw.compass;
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

  const api = { create, sanitize, DEFAULTS, KEY };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    let storage = null;
    try { storage = root.localStorage; } catch (err) { /* blocked */ }
    root.settings = create(storage || { getItem: () => null, setItem: () => {} });
    root.settingsLib = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

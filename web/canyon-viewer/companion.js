// Companion mode logic: where the descent track is, how far the next rappel is, and what a
// run records. No DOM and no map library, so it runs in the browser (global `companion`) and
// in Node for tests. Ported from canyon-companion (app/src/core/{geo,progress}.ts and the
// formatters and run-event helpers in app/src/lib), keeping its numbers and export format.
(function (root) {
  "use strict";

  const EARTH_RADIUS_M = 6_371_008.8;
  const rad = (deg) => (deg * Math.PI) / 180;

  // KML tracks are often tens of metres off the real channel, so be generous.
  const ON_TRACK_MIN_M = 150;
  // Don't log a position more often than this, and don't trust one older than FRESH_FIX_MS
  // for "save my location".
  const MIN_RECORD_INTERVAL_MS = 3000;
  const FRESH_FIX_MS = 30_000;

  // ---- geometry (canyon-companion core/geo.ts) ------------------------------------

  // Great-circle distance in metres between [lon, lat] points.
  function distanceM(a, b) {
    const dLat = rad(b[1] - a[1]);
    const dLon = rad(b[0] - a[0]);
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dLon / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(h));
  }

  // Chainage is metres along the track in a local equirectangular frame centred on the
  // track's bounding box (the frame canyon-companion's pipeline uses too).
  class TrackIndex {
    // `coords` are GeoJSON-order [lon, lat, ...] points, ordered start to exit.
    constructor(coords) {
      if (coords.length < 2) throw new Error("A track needs at least two points");
      const lons = coords.map((c) => c[0]);
      const lats = coords.map((c) => c[1]);
      this.lon0 = (Math.min(...lons) + Math.max(...lons)) / 2;
      this.lat0 = (Math.min(...lats) + Math.max(...lats)) / 2;
      this.xs = [];
      this.ys = [];
      this.cum = [0];
      for (let i = 0; i < coords.length; i++) {
        const [x, y] = this.toXY(lons[i], lats[i]);
        this.xs.push(x);
        this.ys.push(y);
        if (i > 0) {
          this.cum.push(this.cum[i - 1] + Math.hypot(x - this.xs[i - 1], y - this.ys[i - 1]));
        }
      }
      this.lengthM = this.cum[this.cum.length - 1];
    }

    toXY(lon, lat) {
      return [
        rad(lon - this.lon0) * Math.cos(rad(this.lat0)) * EARTH_RADIUS_M,
        rad(lat - this.lat0) * EARTH_RADIUS_M,
      ];
    }

    toLonLat(x, y) {
      return [
        this.lon0 + ((x / (EARTH_RADIUS_M * Math.cos(rad(this.lat0)))) * 180) / Math.PI,
        this.lat0 + ((y / EARTH_RADIUS_M) * 180) / Math.PI,
      ];
    }

    // Closest point on the track (clamped to its ends): {chainageM, offsetM, lon, lat}.
    project(lon, lat) {
      const [px, py] = this.toXY(lon, lat);
      let best = { chainageM: 0, offsetM: Infinity, x: this.xs[0], y: this.ys[0] };
      for (let i = 0; i + 1 < this.xs.length; i++) {
        const ax = this.xs[i];
        const ay = this.ys[i];
        const dx = this.xs[i + 1] - ax;
        const dy = this.ys[i + 1] - ay;
        const len2 = dx * dx + dy * dy;
        const t = len2 === 0 ? 0 : Math.min(1, Math.max(0, ((px - ax) * dx + (py - ay) * dy) / len2));
        const cx = ax + t * dx;
        const cy = ay + t * dy;
        const offset = Math.hypot(px - cx, py - cy);
        if (offset < best.offsetM) {
          best = { chainageM: this.cum[i] + t * Math.sqrt(len2), offsetM: offset, x: cx, y: cy };
        }
      }
      const [lo, la] = this.toLonLat(best.x, best.y);
      return { chainageM: best.chainageM, offsetM: best.offsetM, lon: lo, lat: la };
    }
  }

  // ---- progress (core/progress.ts) --------------------------------------------------

  // A position the user recorded in the field beats the GeoPackage's. `track` may be null
  // (no usable descent track), in which case there is no chainage. With neither position the
  // rappel's location is unknown and no distance can be shown.
  function resolveTarget(track, bundled, recorded) {
    if (recorded) {
      return {
        position: { lon: recorded.lon, lat: recorded.lat },
        chainageM: track ? track.project(recorded.lon, recorded.lat).chainageM : null,
      };
    }
    if (bundled && bundled.location) {
      const { lon, lat } = bundled.location;
      return {
        position: { lon, lat },
        chainageM: bundled.chainageM ?? (track ? track.project(lon, lat).chainageM : null),
      };
    }
    return { position: null, chainageM: null };
  }

  // Distance from the user to a rappel: along the track when the user is on it (a winding
  // canyon is not shortcut through the walls), straight-line when off it or without a track,
  // null when the rappel's position is unknown. Negative along-track metres mean "passed".
  function distanceToRappel(track, fix, target) {
    if (!target.position) return null;
    if (track && target.chainageM !== null) {
      const here = track.project(fix.lon, fix.lat);
      if (here.offsetM <= Math.max(ON_TRACK_MIN_M, 2 * fix.acc)) {
        return { kind: "along-track", meters: target.chainageM - here.chainageM };
      }
    }
    return {
      kind: "straight",
      meters: distanceM([fix.lon, fix.lat], [target.position.lon, target.position.lat]),
    };
  }

  // ---- choosing the track and rappels from loadCanyon() ---------------------------------

  // LineString or MultiLineString GeoJSON geometry -> one coordinate list (or null).
  function lineCoordinates(geometry) {
    if (!geometry) return null;
    if (geometry.type === "LineString") return geometry.coordinates;
    if (geometry.type === "MultiLineString") return geometry.coordinates.flat();
    return null;
  }

  // The route that serves as the descent track: the longest 'descent' route, else the longest
  // 'unknown' one. Approach, exit and road routes are never used. Returns
  // {coordinates, name, kind} or null when there is none with two or more points.
  function pickTrack(routes) {
    const usable = (kind) => routes
      .filter((r) => r.kind === kind)
      .map((r) => ({ coordinates: lineCoordinates(r.line), name: r.name, kind: r.kind }))
      .filter((r) => r.coordinates && r.coordinates.length >= 2)
      .sort((a, b) => b.coordinates.length - a.coordinates.length);
    return usable("descent")[0] || usable("unknown")[0] || null;
  }

  // Rappels in descent order, shaped for the companion. The id is the GeoPackage's rappel
  // `sequence` (stable across rebuilds, unlike row ids).
  function planRappels(rappels) {
    return rappels.slice().sort((a, b) => a.sequence - b.sequence).map((r) => ({
      id: String(r.sequence),
      order: r.sequence,
      name: r.label,
      heightM: r.height_m,
      notes: r.notes,
      location: r.point ? { lon: r.point.coordinates[0], lat: r.point.coordinates[1] } : null,
    }));
  }

  // ---- run records ----------------------------------------------------------------------

  // Something the user did, with the place and time (location null without GPS).
  function makeEvent(run, type, fix, rappel, now = Date.now()) {
    return {
      runId: run.id,
      canyonId: run.canyonId,
      t: fix ? fix.t : now,
      type,
      rappelId: rappel ? rappel.id : null,
      rappelOrder: rappel ? rappel.order : null,
      lat: fix ? fix.lat : null,
      lon: fix ? fix.lon : null,
      acc: fix ? fix.acc : null,
    };
  }

  // Latest user-recorded position per rappel id from "mark" events, across every run.
  function recordedFromMarks(events) {
    const marks = events.filter((e) => e.type === "mark").sort((a, b) => a.t - b.t);
    const out = new Map();
    for (const m of marks) {
      if (m.rappelId && m.lat !== null && m.lon !== null) {
        out.set(m.rappelId, { lon: m.lon, lat: m.lat, acc: m.acc ?? 0, t: m.t });
      }
    }
    return out;
  }

  // The document "Export run" downloads (canyon-companion's run format, version 1).
  function exportDocument(run, events, fixes) {
    const byTime = (a, b) => a.t - b.t;
    return {
      format: "canyon-companion-run",
      version: 1,
      source: "canyongis-viewer",
      run,
      events: events.slice().sort(byTime),
      fixes: fixes.slice().sort(byTime),
    };
  }

  // ---- compass heading ---------------------------------------------------------------------

  const CARDINALS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const norm360 = (deg) => ((deg % 360) + 360) % 360;

  function cardinal(heading) {
    return CARDINALS[Math.round(norm360(heading) / 45) % 8];
  }

  // Compass heading (degrees clockwise from north) of the way the device is pointing, from
  // W3C orientation angles alpha/beta/gamma (the frame is x east, y north, z up). A flat device
  // reports where its top points, an upright one where its back points (the way you face);
  // between the two, whichever has more horizontal length is used. Null when alpha is missing.
  function headingFromOrientation(alpha, beta, gamma) {
    if (alpha === null || alpha === undefined) return null;
    const a = rad(alpha);
    const b = rad(beta || 0);
    const g = rad(gamma || 0);
    const topE = -Math.sin(a) * Math.cos(b);
    const topN = Math.cos(a) * Math.cos(b);
    const backE = -(Math.sin(g) * Math.cos(a) + Math.cos(g) * Math.sin(b) * Math.sin(a));
    const backN = -(Math.sin(g) * Math.sin(a) - Math.cos(g) * Math.sin(b) * Math.cos(a));
    const useTop = Math.hypot(topE, topN) >= Math.hypot(backE, backN);
    const [e, n] = useTop ? [topE, topN] : [backE, backN];
    if (Math.hypot(e, n) < 1e-6) return null;
    return norm360((Math.atan2(e, n) * 180) / Math.PI);
  }

  // Heading of the screen's "up" direction: a rotated screen (landscape) turns it by the
  // screen orientation angle.
  function screenHeading(deviceHeading, screenAngle) {
    return deviceHeading === null ? null : norm360(deviceHeading + (screenAngle || 0));
  }

  // ---- formatting (lib/format.ts) ----------------------------------------------------------

  const FEET_PER_METRE = 1 / 0.3048;
  const METRES_PER_MILE = 1609.344;

  // `units` is "metric" (default) or "imperial".
  function formatDistance(meters, units = "metric") {
    const m = Math.abs(meters);
    if (units === "imperial") {
      return m * FEET_PER_METRE >= 1000
        ? `${(m / METRES_PER_MILE).toFixed(2)} mi` : `${Math.round(m * FEET_PER_METRE)} ft`;
    }
    return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
  }

  // A height or short length: whole metres or feet.
  function formatHeight(meters, units = "metric") {
    return units === "imperial" ? `${Math.round(meters * FEET_PER_METRE)} ft` : `${Math.round(meters)} m`;
  }

  function formatKilometres(km, units = "metric") {
    return units === "imperial" ? `${(km * 1000 / METRES_PER_MILE).toFixed(1)} mi` : `${km.toFixed(1)} km`;
  }

  function formatDuration(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const pad = (n) => String(n).padStart(2, "0");
    return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  function formatAge(ms) {
    const s = Math.round(ms / 1000);
    return s < 60 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
  }

  const api = {
    ON_TRACK_MIN_M, MIN_RECORD_INTERVAL_MS, FRESH_FIX_MS,
    distanceM, TrackIndex, resolveTarget, distanceToRappel,
    lineCoordinates, pickTrack, planRappels,
    makeEvent, recordedFromMarks, exportDocument,
    formatDistance, formatHeight, formatKilometres, formatDuration, formatAge,
    cardinal, headingFromOrientation, screenHeading,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.companion = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

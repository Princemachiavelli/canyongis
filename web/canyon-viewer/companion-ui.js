// Companion mode screen: live GPS guidance down the selected canyon. Starts a run, shows the
// next rappel and how far away it is, and records what the user does (next / back / save my
// location / finish) in IndexedDB. Reuses the page's single MapLibre map by moving it into this
// view while a run is going. The logic lives in companion.js, storage in companion-store.js.
(function (root) {
  "use strict";

  const lib = root.companion;

  // deps: { el, $, map, goTo(viewName) }
  function create(deps) {
    const { el, $, map, whenStyleReady, fitPoints, labelImage, goTo } = deps;
    const top = $("companion-top");
    const slot = $("companion-map-slot");
    const actions = $("companion-actions");
    const mapEl = map.getContainer();
    const mapHome = $("view-map");
    // Everything on the map for a run is a layer, so it sits on the terrain like the KML's track
    // does (DOM markers drift away from it when the view moves): the run's rappels, the position
    // with its accuracy circle, and the heading wedge.
    const EMPTY = { type: "FeatureCollection", features: [] };
    let runNodeCount = 0;
    // The wedge is drawn once into an image (it points up; the layer rotates it).
    function wedgeImage() {
      const size = 44 * 2;
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext("2d");
      ctx.scale(2, 2);
      ctx.lineJoin = "round";
      ctx.lineWidth = 2;
      ctx.fillStyle = "#3d8bfd";
      ctx.strokeStyle = "#fff";
      ctx.beginPath();
      ctx.moveTo(22, 2);
      ctx.lineTo(31, 17);
      ctx.lineTo(22, 13);
      ctx.lineTo(13, 17);
      ctx.closePath();
      ctx.stroke();
      ctx.fill();
      return ctx.getImageData(0, 0, size, size);
    }
    whenStyleReady(() => {
      map.addSource("run-nodes", { type: "geojson", data: EMPTY });
      map.addSource("run-position", { type: "geojson", data: EMPTY });
      map.addImage("heading-wedge", wedgeImage(), { pixelRatio: 2 });
      const part = (name) => ["==", ["get", "part"], name];
      // the rappel to go to next has a gold ring and its name; ones saved in the field are green
      map.addLayer({
        id: "run-nodes", type: "circle", source: "run-nodes",
        paint: {
          "circle-radius": ["case", ["get", "active"], 9, 6],
          "circle-color": ["case", ["get", "recorded"], "#7bd88f", "#f3e9dc"],
          "circle-stroke-color": ["case", ["get", "active"], "#ffd166", "#7c3f1d"],
          "circle-stroke-width": ["case", ["get", "active"], 3, 2],
        },
      });
      map.addLayer({
        id: "run-labels", type: "symbol", source: "run-nodes", filter: ["has", "icon"],
        layout: {
          "icon-image": ["get", "icon"], "icon-anchor": "left", "icon-offset": [14, 0],
          "icon-allow-overlap": true, "icon-ignore-placement": true,
          "icon-pitch-alignment": "viewport", "icon-rotation-alignment": "viewport",
        },
      });
      map.addLayer({
        id: "user-accuracy", type: "fill", source: "run-position", filter: part("accuracy"),
        paint: { "fill-color": "#3d8bfd", "fill-opacity": 0.12 },
      });
      map.addLayer({
        id: "user-accuracy-edge", type: "line", source: "run-position", filter: part("accuracy"),
        paint: { "line-color": "#3d8bfd", "line-width": 1 },
      });
      map.addLayer({
        id: "user-dot", type: "circle", source: "run-position", filter: part("dot"),
        paint: {
          "circle-radius": 8, "circle-color": "#3d8bfd", "circle-stroke-color": "#fff",
          "circle-stroke-width": 3,
        },
      });
      // Rotated to the compass heading on the ground (not the screen), so it stays right when
      // the map is turned or tilted.
      map.addLayer({
        id: "user-wedge", type: "symbol", source: "run-position", filter: part("wedge"),
        layout: {
          "icon-image": "heading-wedge", "icon-rotate": ["get", "heading"],
          "icon-rotation-alignment": "map", "icon-pitch-alignment": "map",
          "icon-allow-overlap": true, "icon-ignore-placement": true,
        },
      });
    });

    // A circle of `radius` metres around a point, as a polygon.
    function circlePolygon(lon, lat, radius, steps = 48) {
      const dLat = radius / 111320;
      const dLon = dLat / Math.max(0.01, Math.cos((lat * Math.PI) / 180));
      const ring = [];
      for (let i = 0; i <= steps; i += 1) {
        const a = (i / steps) * 2 * Math.PI;
        ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
      }
      return { type: "Polygon", coordinates: [ring] };
    }

    // The position, its accuracy circle and (with the compass on and a heading) the wedge.
    function setUserSource(position) {
      const heading = position && compassOn() ? currentHeading() : null;
      const features = position ? [
        { type: "Feature", properties: { part: "accuracy" },
          geometry: circlePolygon(position.lon, position.lat, position.acc || 0) },
        { type: "Feature", properties: { part: "dot" },
          geometry: { type: "Point", coordinates: [position.lon, position.lat] } },
        ...(heading === null ? [] : [{
          type: "Feature", properties: { part: "wedge", heading },
          geometry: { type: "Point", coordinates: [position.lon, position.lat] } }]),
      ] : [];
      whenStyleReady(() => map.getSource("run-position").setData({ type: "FeatureCollection", features }));
    }

    let currentView = "canyons";
    let detail = null;            // canyon selected in the Canyons tab (loadCanyon result)
    let session = null;           // the run in progress or just ended: {detail, title, track, rappels, run}
    let recorded = new Map();     // rappel id -> position saved in the field
    let idle = { resumable: null, past: [], loaded: false, error: null };
    let idleToken = 0;
    let storePromise = null;
    let fix = null;
    let latest = null;
    let geoError = null;
    let follow = false;
    let notice = null;
    let lastRecorded = 0;
    let watchId = null;
    let clock = null;
    let orientation = null;       // compass heading of the screen's "up", from the device sensor
    let headingFrame = 0;
    let ownMove = false;          // true while this code (not the user) moves the map
    let locateBox = null;
    let locateBtn = null;
    const open = { notes: false, rappels: false };
    const refs = {};

    // The page shares one store with the forecast and GeoPackage-saving code (deps.getStore).
    const getStore = deps.getStore || (() => {
      storePromise = storePromise || root.companionStore.openStore(lib);
      return storePromise;
    });
    const running = () => Boolean(session && session.run.endedAt === null);
    const units = () => settings.get().units;

    // ---- map placement -------------------------------------------------------------

    // The map's slot changes height as the cards above and below it change.
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => map.resize()).observe(slot);
    }

    // The map lives in the Map view, except while a run is active and this view is showing.
    function placeMap() {
      const here = currentView === "companion" && running();
      slot.hidden = !here;
      const host = here ? slot : mapHome;
      if (mapEl.parentNode !== host) host.append(mapEl);
      if (currentView === "companion" || currentView === "map") map.resize();
    }

    // Run a map move made by the app itself, so it is not mistaken for the user adjusting the map.
    function ownMapMove(action) {
      ownMove = true;
      try { action(); } finally { ownMove = false; }
    }

    function fitTrack() {
      if (!session) return;
      const points = session.track ? session.track.coordinates.map(([lon, lat]) => [lon, lat])
        : session.rappels.filter((r) => r.location).map((r) => [r.location.lon, r.location.lat]);
      if (points.length) {
        ownMapMove(() => fitPoints(points, { padding: 24, maxZoom: 16, duration: 0 }));
      }
    }

    // ---- follow mode: a locate button on the map -----------------------------------------
    // While on, the map re-centres on each GPS fix. Dragging or zooming the map turns it off.

    const LOCATE_ICON = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" '
      + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
      + '<circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/>'
      + '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>';

    locateBox = el("div", { class: "locate-control maplibregl-ctrl map-bar" });
    locateBtn = el("button", {
      type: "button", class: "locate-btn", title: "Follow my location",
      "aria-label": "Follow my location", "aria-pressed": "false",
    });
    locateBtn.innerHTML = LOCATE_ICON; // fixed markup above, no user text
    locateBtn.addEventListener("click", () => setFollow(!follow));
    locateBox.append(locateBtn);
    locateBox.hidden = true;
    map.addControl({ onAdd: () => locateBox, onRemove: () => locateBox.remove() }, "bottom-right");

    function setFollow(on) {
      follow = on && running();
      locateBtn.classList.toggle("active", follow);
      locateBtn.setAttribute("aria-pressed", String(follow));
      if (follow && fix) {
        ownMapMove(() => map.jumpTo({ center: [fix.lon, fix.lat], zoom: Math.max(map.getZoom(), 15) }));
      }
    }

    function showLocate() {
      locateBox.hidden = !running();
      if (!running()) setFollow(false);
    }

    map.on("dragstart", () => { if (follow) setFollow(false); });
    // A zoom by touch or wheel has an originalEvent; one made by the app does not.
    map.on("zoomstart", (e) => { if (follow && !ownMove && e.originalEvent) setFollow(false); });

    function drawMap() {
      setUserSource(null);
      const nodes = [];
      if (session) {
        const active = activeRappel();
        for (const r of session.rappels) {
          const rec = recorded.get(r.id);
          const pos = rec || r.location;
          if (!pos) continue;
          nodes.push({
            name: r.name || `R${r.order}`, active: Boolean(active && r.id === active.id),
            recorded: Boolean(rec), coordinates: [pos.lon, pos.lat],
          });
        }
      }
      runNodeCount = nodes.length;
      whenStyleReady(() => map.getSource("run-nodes").setData({
        type: "FeatureCollection",
        features: nodes.map(({ coordinates, ...properties }) => ({
          type: "Feature",
          properties: properties.active ? { ...properties, icon: labelImage(properties.name) } : properties,
          geometry: { type: "Point", coordinates },
        })),
      }));
      if (session) drawUser();
    }

    function drawUser() {
      setUserSource(fix && running() ? fix : null);
    }

    // ---- compass ---------------------------------------------------------------------------
    // The heading comes from the device's absolute orientation (webkitCompassHeading on iOS);
    // a relative alpha is no use as a compass. With no sensor reading, the GPS heading is used
    // while moving. Only the wedge's rotation is updated, once per animation frame.

    const compassOn = () => settings.get().compass && running();

    function currentHeading() {
      if (orientation !== null) return orientation;
      if (fix && typeof fix.heading === "number" && !Number.isNaN(fix.heading)
          && typeof fix.speed === "number" && fix.speed > 0.5) return fix.heading;
      return null;
    }

    function applyHeading() {
      headingFrame = 0;
      if (compassOn()) drawUser();
    }

    function scheduleHeading() {
      if (!headingFrame) headingFrame = requestAnimationFrame(applyHeading);
    }

    function onOrientation(event) {
      const angle = (screen.orientation && screen.orientation.angle) || 0;
      let h = null;
      if (typeof event.webkitCompassHeading === "number") {
        h = lib.screenHeading(event.webkitCompassHeading, angle);
      } else if (event.absolute) {
        h = lib.screenHeading(lib.headingFromOrientation(event.alpha, event.beta, event.gamma), angle);
      }
      if (h === null) return;
      orientation = h;
      scheduleHeading();
    }

    let compassListening = false;
    function syncCompass() {
      const want = compassOn();
      if (want && !compassListening) {
        window.addEventListener("deviceorientationabsolute", onOrientation);
        window.addEventListener("deviceorientation", onOrientation);
      } else if (!want && compassListening) {
        window.removeEventListener("deviceorientationabsolute", onOrientation);
        window.removeEventListener("deviceorientation", onOrientation);
        orientation = null;
      }
      compassListening = want;
      drawUser();
      updateLive();
    }

    // ---- GPS -------------------------------------------------------------------------

    const toFix = (p) => ({
      lon: p.coords.longitude, lat: p.coords.latitude, acc: p.coords.accuracy, t: p.timestamp,
      heading: p.coords.heading, speed: p.coords.speed,
    });

    function accept(p) {
      const f = toFix(p);
      latest = f;
      fix = f;
      geoError = null;
      if (running() && f.t - lastRecorded >= lib.MIN_RECORD_INTERVAL_MS) {
        lastRecorded = f.t;
        getStore().then((store) => store.addFix(session.run.id, f)).catch(console.error);
      }
      drawUser();
      if (follow && (currentView === "companion" || currentView === "map")) {
        ownMapMove(() => map.jumpTo({ center: [f.lon, f.lat] }));
      }
      updateLive();
    }

    function geoFailed(e) {
      geoError = e.code === e.PERMISSION_DENIED ? "Location permission denied" : e.message;
      updateLive();
    }

    const GEO_OPTIONS = { enableHighAccuracy: true, maximumAge: 0, timeout: 30_000 };

    function onVisible() {
      if (document.visibilityState !== "visible" || watchId === null) return;
      navigator.geolocation.clearWatch(watchId);
      watchId = navigator.geolocation.watchPosition(accept, geoFailed, GEO_OPTIONS);
      navigator.geolocation.getCurrentPosition(accept, geoFailed, GEO_OPTIONS);
    }

    // Phones suspend pages while locked, so nothing is recorded then; when the page is visible
    // again the watch restarts and a fix is requested right away.
    function startGeo() {
      stopGeo();
      fix = null;
      latest = null;
      geoError = null;
      lastRecorded = 0;
      if (!window.isSecureContext) {
        geoError = "GPS needs HTTPS or localhost; this page was not loaded from either";
      } else if (!("geolocation" in navigator)) {
        geoError = "This device has no geolocation support";
      } else {
        watchId = navigator.geolocation.watchPosition(accept, geoFailed, GEO_OPTIONS);
        document.addEventListener("visibilitychange", onVisible);
      }
      clock = setInterval(updateLive, 1000);
    }

    function stopGeo() {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
      watchId = null;
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(clock);
      clock = null;
    }

    // A fix no older than FRESH_FIX_MS, asking the device for a new one if needed.
    function freshFix() {
      return new Promise((resolve, reject) => {
        if (latest && Date.now() - latest.t < lib.FRESH_FIX_MS) { resolve(latest); return; }
        if (!window.isSecureContext || !("geolocation" in navigator)) {
          reject(new Error("No GPS available")); return;
        }
        navigator.geolocation.getCurrentPosition(
          (p) => { accept(p); resolve(toFix(p)); },
          (e) => reject(new Error(e.message || "Could not get a GPS fix")),
          { enableHighAccuracy: true, maximumAge: 0, timeout: 15_000 },
        );
      });
    }

    // ---- run state ---------------------------------------------------------------------

    function activeRappel() {
      if (!session || session.run.nextIndex >= session.rappels.length) return null;
      return session.rappels[session.run.nextIndex];
    }

    function targetFor(rappel) {
      return lib.resolveTarget(session.track && session.track.index,
        { location: rappel.location, chainageM: null }, recorded.get(rappel.id));
    }

    function buildSession(d, run) {
      const picked = lib.pickTrack(d.routes);
      return {
        detail: d,
        title: d.canyon.title,
        track: picked ? { ...picked, index: new lib.TrackIndex(picked.coordinates) } : null,
        rappels: lib.planRappels(d.rappels),
        run,
      };
    }

    async function begin(run, d) {
      const store = await getStore();
      recorded = await store.recordedPositions(d.canyon.title);
      session = buildSession(d, run);
      notice = null;
      try { await navigator.storage?.persist?.(); } catch (err) { /* best effort */ }
      if (run.endedAt === null) startGeo();
      render(); // first: its height decides how much room the map gets
      placeMap();
      drawMap();
      showLocate();
      syncCompass();
      if (running()) fitTrack();
    }

    async function start() {
      try {
        const store = await getStore();
        const run = await store.startRun(detail.canyon.title, null);
        await begin(run, detail);
      } catch (err) {
        idle.error = err.message;
        render();
      }
    }

    async function go(type, index) {
      const from = activeRappel();
      const total = session.rappels.length;
      const store = await getStore();
      await store.moveTo(session.run, type, Math.min(Math.max(index, 0), total),
        from ? { id: from.id, order: from.order } : null, fix);
      notice = null;
      drawMap();
      render();
    }

    async function saveLocation() {
      const rappel = activeRappel();
      if (!rappel) return;
      if (!confirm(`Save your current GPS position as the location of ${rappel.name || `rappel ${rappel.order}`}?`
        + " It will replace the position used for distances.")) return;
      try {
        const f = await freshFix();
        const store = await getStore();
        await store.markRappelLocation(session.run, { id: rappel.id, order: rappel.order }, f);
        recorded = await store.recordedPositions(session.title);
        notice = `Saved location for rappel ${rappel.order} (±${Math.round(f.acc)} m)`;
        drawMap();
      } catch (err) {
        notice = `Could not save location: ${err.message}`;
      }
      render();
    }

    async function finish() {
      if (!confirm(`Finish ${session.title}? This ends the run and stops GPS recording.`)) return;
      const store = await getStore();
      await store.finishRun(session.run, fix);
      stopGeo();
      placeMap();
      drawMap();
      showLocate();
      syncCompass();
      render();
    }

    // Delete a run, after confirming. Positions saved during it go with it, so offer an export first.
    async function removeRun(run) {
      const when = new Date(run.startedAt).toLocaleString();
      if (!confirm(`Delete the run from ${when}? Its GPS track and any rappel positions saved `
        + "during it are removed too. Export it first if you want a copy.")) return;
      try {
        const store = await getStore();
        await store.deleteRun(run.id);
      } catch (err) {
        idle.error = err.message;
      }
      await refreshIdle();
    }

    async function exportRun(runId) {
      const store = await getStore();
      const doc = await store.exportRun(runId);
      await root.share.deliverFile(root.share.makeFile(
        `run-${runId}.json`, JSON.stringify(doc, null, 2), "application/json"));
    }

    function leave() {
      session = null;
      recorded = new Map();
      fix = null;
      drawMap();
      placeMap();
      showLocate();
      syncCompass();
      refreshIdle();
    }

    // Unfinished and past runs of the selected canyon, for the start screen.
    async function refreshIdle() {
      const token = ++idleToken;
      idle = { resumable: null, past: [], loaded: false, error: null };
      render();
      if (!detail) return;
      try {
        const store = await getStore();
        const runs = await store.runsFor(detail.canyon.title);
        if (token !== idleToken) return;
        idle = {
          resumable: runs.find((r) => r.endedAt === null) || null,
          past: runs.filter((r) => r.endedAt !== null),
          loaded: true,
          error: null,
        };
      } catch (err) {
        if (token !== idleToken) return;
        idle.error = err.message;
      }
      render();
    }

    // ---- rendering ---------------------------------------------------------------------

    // Boolean attributes: true sets them, false leaves them off.
    const button = (label, onClick, cls, attrs) => {
      const given = Object.fromEntries(Object.entries(attrs || {}).map(([k, v]) =>
        [k, v === true ? "" : v === false ? null : v]));
      const b = el("button", { type: "button", class: cls || null, ...given }, [label]);
      b.addEventListener("click", onClick);
      return b;
    };
    // replaceChildren would turn a null child into the text "null".
    const fill = (node, children) => node.replaceChildren(...children.filter((c) => c));

    function render() {
      for (const k of Object.keys(refs)) delete refs[k];
      if (session) renderSession();
      else if (detail) renderStart();
      else renderEmpty();
      updateLive();
    }

    function renderEmpty() {
      fill(top, [
        el("h2", {}, ["Companion"]),
        el("p", { class: "muted" }, [
          "Pick a canyon in the Canyons tab, then start companion mode here to follow your "
          + "position down the canyon, rappel by rappel.",
        ]),
        button("Choose a canyon", () => goTo("canyons"), "primary"),
      ]);
      fill(actions, []);
    }

    function renderStart() {
      const rappels = lib.planRappels(detail.rappels);
      const picked = lib.pickTrack(detail.routes);
      const unlocated = rappels.filter((r) => !r.location).length;
      const items = [];
      if (picked) {
        items.push(`Descent track: ${picked.name || "unnamed"}.`);
      } else {
        items.push("No descent track in this file: distances will be straight-line only.");
      }
      if (!rappels.length) items.push("No rappels are listed for this canyon.");
      else if (unlocated) {
        items.push(`${unlocated} of ${rappels.length} rappels have no position; save your location `
          + "at the anchor to add one.");
      }
      if (!window.isSecureContext) {
        items.push("GPS needs HTTPS or localhost. Open this page that way to get a position.");
      }
      fill(top, [
        el("h2", {}, [detail.canyon.title]),
        el("p", { class: "muted" }, [`${rappels.length} rappels`]),
        el("ul", { class: "plain" }, items.map((t) => el("li", {}, [t]))),
        idle.error ? el("p", { class: "notice" }, [idle.error]) : null,
      ]);
      const resume = idle.resumable ? el("div", { class: "row" }, [
        button(
          `Resume run (started ${new Date(idle.resumable.startedAt).toLocaleTimeString()})`,
          () => begin(idle.resumable, detail).catch((err) => { idle.error = err.message; render(); }),
          "primary"),
        button("Delete", () => removeRun(idle.resumable), "small danger",
          { title: "Delete this unfinished run" }),
      ]) : null;
      const label = (r) => `${new Date(r.startedAt).toLocaleString([], {
        dateStyle: "short", timeStyle: "short" })} · ${lib.formatDuration(r.endedAt - r.startedAt)}`;
      fill(actions, [
        resume,
        button("Start canyon", start, idle.resumable ? "" : "primary", { disabled: !idle.loaded && !idle.error }),
        idle.past.length ? el("h3", { class: "muted past-title" }, ["Previous runs"]) : null,
        ...idle.past.map((r) => el("div", { class: "row" }, [
          button(`Export ${label(r)}`, () => exportRun(r.id), "small wide"),
          button("Delete", () => removeRun(r), "small danger", { title: "Delete this run" }),
        ])),
      ]);
    }

    function renderSession() {
      const { rappels, run } = session;
      const total = rappels.length;
      const rappel = activeRappel();
      const ended = run.endedAt !== null;
      refs.clock = el("span", { class: "mono" });
      refs.gps = el("div", { class: "gps" });
      refs.distance = el("div", { class: "distance" });
      refs.weather = el("div", { class: "weather-icons" });

      let card;
      if (rappel) {
        // One row: which rappel, how far, how tall. Notes open in a full-width line below.
        let notesToggle = null;
        let notesText = null;
        if (rappel.notes) {
          notesText = el("p", { class: "rappel-notes" }, [rappel.notes]);
          notesText.hidden = !open.notes;
          notesToggle = el("button", { type: "button", class: "link" },
            [open.notes ? "Notes ▴" : "Notes ▾"]);
          notesToggle.addEventListener("click", () => {
            open.notes = !open.notes;
            notesText.hidden = !open.notes;
            notesToggle.textContent = open.notes ? "Notes ▴" : "Notes ▾";
          });
        }
        card = el("section", { class: "card" }, [
          el("div", { class: "card-grid" }, [
            el("div", { class: "cell-main" }, [
              el("div", {}, [el("span", { class: "muted" }, [`Next rappel ${rappel.order}/${total} · `]),
                el("b", {}, [rappel.name || `R${rappel.order}`])]),
              notesToggle,
            ]),
            refs.distance,
            el("div", { class: "height-cell" }, [
              el("span", { class: "muted" }, ["Height"]),
              el("span", {}, [rappel.heightM ? lib.formatHeight(rappel.heightM, units()) : "not stated"]),
            ]),
          ]),
          notesText,
        ]);
      } else {
        card = el("section", { class: "card" }, [
          el("h3", {}, [total === 0 ? "No rappels listed" : "All rappels done"]),
          ended ? null : el("p", { class: "muted" }, ["Finish the run when you are out of the canyon."]),
        ]);
      }
      fill(top, [
        el("div", { class: "companion-head" }, [
          el("h2", {}, [session.title]),
          el("div", { class: "clock-col" }, [refs.clock, refs.weather]),
        ]),
        refs.gps,
        card,
        session.track ? null : el("p", { class: "muted" }, ["No descent track: distances are straight-line."]),
      ]);
      drawWeather();

      if (ended) {
        fill(actions, [
          button("Export run", () => exportRun(run.id), "primary"),
          button("Done", leave),
        ]);
        return;
      }
      const list = total ? el("ol", { class: "plain" }, rappels.map((r, i) => el("li", {}, [
        button(`${r.order}. ${r.name || "Rappel"}${r.heightM ? ` · ${lib.formatHeight(r.heightM, units())}` : ""}`
          + `${recorded.has(r.id) ? " ✓" : ""}`, () => go("jump", i),
        i === run.nextIndex ? "current" : ""),
      ]))) : null;
      if (list) list.hidden = !open.rappels;
      const listToggle = total ? button(open.rappels ? "All rappels ▴" : "All rappels ▾", () => {
        open.rappels = !open.rappels;
        list.hidden = !open.rappels;
        listToggle.textContent = open.rappels ? "All rappels ▴" : "All rappels ▾";
      }, "small") : null;
      const saveButton = rappel ? button(`📍 Save as ${rappel.name || `R${rappel.order}`}`,
        saveLocation, "small save", {
          title: `Save my location as ${rappel.name || `rappel ${rappel.order}`}`,
        }) : null;
      fill(actions, [
        notice ? el("p", { class: "notice" }, [notice]) : null,
        el("div", { class: "row" }, [
          button("‹ Back", () => go("back", run.nextIndex - 1), "", { disabled: run.nextIndex <= 0 }),
          button("Next ›", () => go("next", run.nextIndex + 1), "primary",
            { disabled: run.nextIndex >= total }),
          saveButton,
        ]),
        el("div", { class: "row" }, [listToggle, button("Finish canyon", finish, "small")]),
        list,
      ]);
    }

    // Clock, GPS line and distance: refreshed every second and on every fix, without
    // rebuilding the screen (that would collapse the open <details> sections).
    function updateLive() {
      if (!session || !refs.gps) return;
      const { run } = session;
      const now = Date.now();
      const ended = run.endedAt !== null;
      refs.clock.textContent = lib.formatDuration((run.endedAt ?? now) - run.startedAt);
      const bad = Boolean(geoError) || !fix;
      refs.gps.className = `gps${bad && !ended ? " bad" : ""}`;
      const heading = compassOn() ? currentHeading() : null;
      const headingText = heading === null ? ""
        : ` · ${Math.round(heading)}° ${lib.cardinal(heading)}`;
      refs.gps.textContent = geoError ? geoError
        : fix && !ended ? `GPS ±${Math.round(fix.acc)} m · ${lib.formatAge(now - fix.t)}${headingText}`
        : ended ? "Run finished" : "Waiting for GPS…";
      const rappel = activeRappel();
      if (!rappel || !refs.distance) return;
      const target = targetFor(rappel);
      const distance = fix && target.position
        ? lib.distanceToRappel(session.track && session.track.index, fix, target) : null;
      if (distance) {
        refs.distance.title = "";
        refs.distance.replaceChildren(
          el("strong", {}, [lib.formatDistance(distance.meters, units())]),
          el("span", { class: "muted" }, [
            `${distance.meters < 0 ? "behind · " : ""}${distance.kind === "along-track" ? "on track" : "straight"}`
            + `${recorded.has(rappel.id) ? " · ✓" : ""}`]),
        );
      } else {
        refs.distance.title = target.position ? "" : "Use Next, and save the location when you are at the anchor.";
        refs.distance.replaceChildren(
          el("strong", {}, ["—"]),
          el("span", { class: "muted" }, [target.position ? "waiting for GPS" : "position unknown"]));
      }
    }

    // Pictures for today's forecast (weather, and hot / cold) under the stopwatch.
    function drawWeather() {
      if (!session || !refs.weather) return;
      const record = deps.getForecast ? deps.getForecast(session.title) : null;
      const icons = root.forecast.iconsFor(
        root.forecast.dayForToday(record), settings.get(), units());
      refs.weather.replaceChildren(
        ...icons.map((icon) => root.weatherIcons.element(icon.name, icon.label)));
    }

    // ---- hooks for the page ----------------------------------------------------------------

    const controller = {
      // The canyon selected in the Canyons tab changed (null: none, or a new file was opened).
      setCanyon(d) {
        detail = d;
        if (!session) refreshIdle();
      },
      // Settings changed (units, compass): redraw what shows them.
      refresh() {
        syncCompass();
        render();
      },
      // The forecast was loaded or updated.
      refreshWeather: drawWeather,
      onViewChange(name) {
        currentView = name;
        placeMap();
        showLocate();
      },
      // Read-only view of the run for tests and debugging.
      state: () => ({
        session, recorded, fix, geoError, nodes: runNodeCount,
      }),
    };
    root.companionUI.last = controller;
    return controller;
  }

  root.companionUI = { create, last: null };
})(typeof globalThis !== "undefined" ? globalThis : this);

// Companion mode screen: live GPS guidance down the selected canyon. Starts a run, shows the
// next rappel and how far away it is, and records what the user does (next / back / save my
// location / finish) in IndexedDB. Reuses the page's single Leaflet map by moving it into this
// view while a run is going. The logic lives in companion.js, storage in companion-store.js.
(function (root) {
  "use strict";

  const lib = root.companion;

  // deps: { el, $, map, goTo(viewName) }
  function create(deps) {
    const { el, $, map, goTo } = deps;
    const top = $("companion-top");
    const slot = $("companion-map-slot");
    const actions = $("companion-actions");
    const mapEl = map.getContainer();
    const mapHome = $("view-map");
    const layer = L.layerGroup().addTo(map);

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
    let userDot = null;
    let userCircle = null;
    let userWedge = null;
    let orientation = null;       // compass heading of the screen's "up", from the device sensor
    let headingEl = null;         // rotating wedge on the position dot
    let headingFrame = 0;
    let ownMove = false;          // true while this code (not the user) moves the map
    let locateBox = null;
    let locateBtn = null;
    const open = { notes: false, rappels: false };
    const refs = {};

    const getStore = () => {
      storePromise = storePromise || root.companionStore.openStore(lib);
      return storePromise;
    };
    const running = () => Boolean(session && session.run.endedAt === null);
    const units = () => settings.get().units;

    // ---- map placement -------------------------------------------------------------

    // The map's slot changes height as the cards above and below it change.
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => map.invalidateSize()).observe(slot);
    }

    // The map lives in the Map view, except while a run is active and this view is showing.
    function placeMap() {
      const here = currentView === "companion" && running();
      slot.hidden = !here;
      const host = here ? slot : mapHome;
      if (mapEl.parentNode !== host) host.append(mapEl);
      if (currentView === "companion" || currentView === "map") map.invalidateSize();
    }

    // Run a map move made by the app itself, so it is not mistaken for the user adjusting the map.
    function ownMapMove(action) {
      ownMove = true;
      try { action(); } finally { ownMove = false; }
    }

    function fitTrack() {
      if (!session) return;
      const points = session.track ? session.track.coordinates.map(([lon, lat]) => [lat, lon])
        : session.rappels.filter((r) => r.location).map((r) => [r.location.lat, r.location.lon]);
      if (points.length) {
        ownMapMove(() => map.fitBounds(points, { padding: [24, 24], maxZoom: 17, animate: false }));
      }
    }

    // ---- follow mode: a locate button on the map -----------------------------------------
    // While on, the map re-centres on each GPS fix. Dragging or zooming the map turns it off.

    const LOCATE_ICON = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" '
      + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
      + '<circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/>'
      + '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>';

    const locateControl = L.control({ position: "bottomright" });
    locateControl.onAdd = () => {
      locateBox = el("div", { class: "locate-control leaflet-bar" });
      locateBtn = el("button", {
        type: "button", class: "locate-btn", title: "Follow my location",
        "aria-label": "Follow my location", "aria-pressed": "false",
      });
      locateBtn.innerHTML = LOCATE_ICON; // fixed markup above, no user text
      locateBtn.addEventListener("click", () => setFollow(!follow));
      L.DomEvent.disableClickPropagation(locateBox);
      locateBox.append(locateBtn);
      locateBox.hidden = true;
      return locateBox;
    };
    locateControl.addTo(map);

    function setFollow(on) {
      follow = on && running();
      locateBtn.classList.toggle("active", follow);
      locateBtn.setAttribute("aria-pressed", String(follow));
      if (follow && fix) {
        ownMapMove(() => map.setView([fix.lat, fix.lon], Math.max(map.getZoom(), 16),
          { animate: false }));
      }
    }

    function showLocate() {
      locateBox.hidden = !running();
      if (!running()) setFollow(false);
    }

    map.on("dragstart", () => { if (follow) setFollow(false); });
    map.on("zoomstart", () => { if (follow && !ownMove) setFollow(false); });

    function drawMap() {
      layer.clearLayers();
      userDot = null;
      userCircle = null;
      userWedge = null;
      headingEl = null;
      if (!session) return;
      const active = activeRappel();
      for (const r of session.rappels) {
        const rec = recorded.get(r.id);
        const pos = rec || r.location;
        if (!pos) continue;
        const isActive = active && r.id === active.id;
        L.circleMarker([pos.lat, pos.lon], {
          radius: isActive ? 9 : 6,
          color: isActive ? "#ffd166" : "#7c3f1d",
          weight: isActive ? 3 : 2,
          fillColor: rec ? "#7bd88f" : "#f3e9dc",
          fillOpacity: 1,
        }).bindTooltip(r.name || `R${r.order}`, { permanent: Boolean(isActive), direction: "top" })
          .addTo(layer);
      }
      drawUser();
    }

    function drawUser() {
      if (userDot) { layer.removeLayer(userDot); userDot = null; }
      if (userCircle) { layer.removeLayer(userCircle); userCircle = null; }
      if (userWedge) { layer.removeLayer(userWedge); userWedge = null; }
      headingEl = null;
      if (!fix || !running()) return;
      userCircle = L.circle([fix.lat, fix.lon], {
        radius: fix.acc, color: "#3d8bfd", weight: 1, fillOpacity: 0.12, interactive: false,
      }).addTo(layer);
      userDot = L.circleMarker([fix.lat, fix.lon], {
        radius: 8, color: "#fff", weight: 3, fillColor: "#3d8bfd", fillOpacity: 1, interactive: false,
      }).addTo(layer);
      headingEl = null;
      if (compassOn()) {
        const icon = L.divIcon({
          className: "heading-icon", iconSize: [44, 44], iconAnchor: [22, 22],
          html: '<svg class="heading-wedge" viewBox="0 0 44 44" aria-hidden="true">'
            + '<path d="M22 2 L31 17 L22 13 L13 17 Z" fill="#3d8bfd" stroke="#fff" stroke-width="2" '
            + 'stroke-linejoin="round"/></svg>',
        });
        userWedge = L.marker([fix.lat, fix.lon], { icon, interactive: false, keyboard: false }).addTo(layer);
        headingEl = userWedge.getElement().querySelector(".heading-wedge");
        applyHeading();
      }
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
      if (!headingEl) return;
      const h = currentHeading();
      headingEl.style.display = h === null ? "none" : "";
      if (h !== null) headingEl.style.transform = `rotate(${h}deg)`;
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
        ownMapMove(() => map.panTo([f.lat, f.lon], { animate: true, duration: 0.5 }));
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
      const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)],
        { type: "application/json" }));
      const a = el("a", { href: url, download: `run-${runId}.json` });
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
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
        el("div", { class: "companion-head" }, [el("h2", {}, [session.title]), refs.clock]),
        refs.gps,
        card,
        session.track ? null : el("p", { class: "muted" }, ["No descent track: distances are straight-line."]),
      ]);

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
      onViewChange(name) {
        currentView = name;
        placeMap();
        showLocate();
      },
      // Read-only view of the run for tests and debugging.
      state: () => ({
        session, recorded, fix, geoError, layers: layer.getLayers().length,
      }),
    };
    root.companionUI.last = controller;
    return controller;
  }

  root.companionUI = { create, last: null };
})(typeof globalThis !== "undefined" ? globalThis : this);

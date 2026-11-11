// The "Updates" block on a canyon's Overview: which parts of this device's copy are out of date
// (compared with the server's fingerprints), and a button to bring them up to date. Only the
// out-of-date sections are downloaded, as a small delta applied to the open file and kept on the
// device so it is applied again whenever the file is opened. See gpkg-update.js.
(function (root) {
  "use strict";

  const LAYER_LABELS = {
    tiles_osm: "Street map", tiles_imagery: "Aerial imagery", tiles_hillshade: "Hillshade",
    tiles_contours_m: "Contours (m)", tiles_contours_ft: "Contours (ft)", tiles_terrain: "3D elevation",
  };
  const REASONS = { missing: "not on this device", version: "built by older code", newer: "rebuilt on the server" };
  const layerText = (t) => `${LAYER_LABELS[t.layer] || t.layer} (${REASONS[t.reason] || t.reason})`;

  // deps: { el, catalog, update (gpkgUpdate), getDb(), getStore(), onApplied(title), redraw(),
  //         refetch(title, onStage) }  refetch downloads the server's current file for the canyon
  //         (map layers are too big for a delta) and swaps it in, keeping the runs on this device.
  function create({ el, catalog, update, getDb, getStore, onApplied, redraw, refetch }) {
    // canyon title -> {busy, stage, message, notes, tiles: map layers to bring up to date,
    //                  pending: layers the server will rebuild first, checked}
    const states = new Map();
    const stateFor = (title) => {
      if (!states.has(title)) {
        states.set(title, { busy: false, stage: "", message: "", notes: [], tiles: [], pending: [], checked: false });
      }
      return states.get(title);
    };

    // A quick look at what the server holds, so a canyon whose map layers are missing or old shows
    // it without anyone pressing Update. Once per canyon a session; nothing is built by looking.
    async function check(detail) {
      const { title } = detail.canyon;
      const st = stateFor(title);
      if (st.checked || st.busy || root.navigator.onLine === false) return;
      st.checked = true;
      try {
        if (!(await catalog.status())) return;
        const info = await catalog.manifest(title);
        const layers = info && info.map ? info.map.layers : {};
        st.tiles = update.tileUpdates(update.readTileStamps(getDb()), layers);
        st.pending = Object.entries((info && info.map && info.map.pending) || {})
          .map(([layer, reason]) => ({ layer, reason }));
        if (st.tiles.length || st.pending.length) redraw();
      } catch (err) {
        st.checked = false; // try again next time the Overview is shown
      }
    }

    async function reachable(st) {
      if (root.navigator.onLine === false || !(await catalog.status())) {
        st.message = "Can't reach the server. Connect to the network the server is on, then try again.";
        return false;
      }
      return true;
    }

    // Bring the out-of-date sections up to date. `force` makes the server ask Ropewiki again
    // even if it did within the last day (never more than every 5 minutes).
    async function run(detail, force) {
      const { title, id } = detail.canyon;
      const st = stateFor(title);
      if (st.busy) return;
      Object.assign(st, { busy: true, message: "", notes: [], stage: "Contacting the server" });
      redraw();
      try {
        if (await reachable(st)) {
          const have = update.haveDigests(update.readStamps(getDb(), id));
          const started = await catalog.startUpdate(title, have, {
            force, tiles: update.readTileStamps(getDb()),
          });
          const job = await catalog.waitForJob(started.id, {
            onUpdate: (j) => { st.stage = j.stage; redraw(); },
          });
          if (job.state === "failed") throw new Error(job.error || "The update failed.");
          st.notes = job.notes || [];
          const tiles = job.tiles_outdated || [];
          if (tiles.length && refetch) {
            // Map layers do not come as a delta: the whole current file replaces this one (the
            // runs, events and fixes recorded on this device are kept apart from it).
            await refetch(title, (stage) => { st.stage = stage; redraw(); });
            st.tiles = [];
            st.pending = [];
            st.message = `Updated the map: ${tiles.map(layerText).join(", ")}.`;
          } else if (!job.outdated.length) {
            st.message = "Already up to date.";
          } else {
            st.stage = "Downloading the changes";
            redraw();
            const delta = await catalog.getDelta(job.id);
            const result = update.applyDelta(getDb(), delta);
            const labels = job.outdated.filter((o) => result.applied.includes(o.section))
              .map((o) => o.label);
            st.message = `Updated: ${labels.join(", ")}.`;
            try {
              await (await getStore()).savePatches(update.patchesFromDelta(delta));
            } catch (err) {
              st.notes.push(`The changes could not be kept for next time (${err.message}).`);
            }
            onApplied(title);
          }
        }
      } catch (err) {
        st.message = `Could not update: ${err.message}`;
      }
      Object.assign(st, { busy: false, stage: "" });
      redraw();
    }

    function render(detail) {
      const { title, id, fetchedAt } = detail.canyon;
      const st = stateFor(title);
      setTimeout(() => check(detail), 0);
      const db = getDb();
      const stamps = db ? update.readStamps(db, id) : {};
      const button = (label, handler, cls, title) => {
        const b = el("button", {
          type: "button", class: `small${cls ? ` ${cls}` : ""}`, disabled: st.busy ? "" : null, title,
        }, [label]);
        b.addEventListener("click", handler);
        return b;
      };
      return el("section", { class: "updates" }, [
        el("h3", {}, ["Updates"]),
        el("p", { class: "muted" }, [
          Object.keys(stamps).length
            ? `Data fetched ${fetchedAt || "at an unknown time"}.`
            : `Data fetched ${fetchedAt || "at an unknown time"}. This file does not record which `
              + "parts are current yet; the first update brings everything up to date.",
        ]),
        el("div", { class: "fc-head" }, [
          button("Update", () => run(detail, false), "primary",
            "Download what changed since this copy was made"),
          button("Re-check Ropewiki", () => run(detail, true), "",
            "Ask Ropewiki again now, even if the server checked it in the last day"),
        ]),
        st.tiles.length ? el("p", { class: "notice" }, [
          `Map out of date on this device: ${st.tiles.map(layerText).join(", ")}. Update downloads the new map.`,
        ]) : null,
        st.pending.length ? el("p", { class: "muted" }, [
          `The server will rebuild ${st.pending.map((t) => LAYER_LABELS[t.layer] || t.layer).join(", ")} `
          + "(missing or over a year old) when you update; that can take several minutes.",
        ]) : null,
        st.busy ? el("p", { class: "muted" }, [`${st.stage}…`]) : null,
        st.message ? el("p", { class: st.message.startsWith("Could not") || st.message.startsWith("Can't")
          ? "notice" : "" }, [st.message]) : null,
        ...st.notes.map((n) => el("p", { class: "notice" }, [n])),
      ]);
    }

    return { render, run, check, message: (title) => stateFor(title).message };
  }

  root.updatesUI = { create };
})(typeof globalThis !== "undefined" ? globalThis : this);

// "Get a canyon" on the Canyons view: shown only while the server's canyon API answers. Search
// Ropewiki, pick a canyon, watch the server build its GeoPackage, then download it into this
// device's storage and open it. The page needs a network and the server only for this; saved
// files work offline.
(function (root) {
  "use strict";

  const SEARCH_DELAY_MS = 350;
  const MIN_SEARCH_CHARS = 2;
  const SPACE_MARGIN = 1.1;

  const mb = (bytes) => `${(bytes / 1e6).toFixed(bytes >= 1e7 ? 0 : 1)} MB`;
  const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

  // deps: { $, el, catalog, getFileCache(), onDownloaded({bytes, name, label, job}) }
  function create({ $, el, catalog, getFileCache, onDownloaded }) {
    const section = $("get-canyon");
    const search = $("catalog-search");
    const note = $("catalog-note");
    const results = $("catalog-results");
    const panel = $("catalog-job");
    const progress = $("catalog-progress");
    const stage = $("catalog-stage");
    const detail = $("catalog-detail");
    const start = $("catalog-start");
    const cancel = $("catalog-cancel");
    const tilesBox = $("catalog-tiles");
    const tilesNote = $("catalog-tiles-note");

    let selected = null;
    let controller = null;
    let busy = false;
    let serverTiles = true;
    let searchToken = 0;
    let timer = null;

    const show = (text) => { note.textContent = text || ""; };

    // ---- is the server reachable? ----------------------------------------------------------
    async function check() {
      if (busy) return;
      const status = root.navigator.onLine === false ? null : await catalog.status();
      section.hidden = !status;
      if (status) {
        serverTiles = status.tiles !== false;
        tilesBox.closest("label").hidden = !serverTiles;
      } else {
        resetSelection();
      }
    }

    // ---- search ------------------------------------------------------------------------------
    function resultLine(r) {
      const meta = [r.region, r.technical_rating ? `T${r.technical_rating}` : null, r.water_rating,
        r.rappels !== null && r.rappels !== undefined ? `${r.rappels} rappels` : null]
        .filter(Boolean).join(" · ");
      const badges = [r.map_ready ? "map ready" : null, r.fresh ? "up to date" : null].filter(Boolean);
      const item = el("li", { "data-title": r.title }, [
        el("div", {}, [r.title, ...badges.map((b) => el("span", { class: "badge" }, [b]))]),
        el("div", { class: "meta" }, [meta]),
      ]);
      item.addEventListener("click", () => select(r));
      return item;
    }

    async function runSearch() {
      const text = search.value.trim();
      const token = ++searchToken;
      results.replaceChildren();
      if (text.length < MIN_SEARCH_CHARS) { show(""); return; }
      show("Searching Ropewiki…");
      try {
        const found = await catalog.search(text);
        if (token !== searchToken) return;
        show(found.length ? "" : "No Ropewiki canyons match.");
        results.replaceChildren(...found.map(resultLine));
      } catch (err) {
        if (token === searchToken) show(`Search failed: ${err.message}`);
      }
    }

    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(runSearch, SEARCH_DELAY_MS);
    });

    // ---- one canyon: options, build, download ---------------------------------------------
    function select(r) {
      if (busy) return;
      selected = r;
      panel.hidden = false;
      $("catalog-job-title").textContent = r.title;
      tilesNote.textContent = r.map_ready
        ? "The map for this canyon is already prepared on the server."
        : "US canyons only. The first time can take many minutes and download several GB on "
          + "the server; elsewhere you get the canyon data without a map.";
      stage.textContent = "";
      detail.textContent = "";
      progress.hidden = true;
      start.hidden = false;
      start.textContent = "Get GeoPackage";
      start.disabled = false;
      cancel.textContent = "Close";
    }

    function resetSelection() {
      selected = null;
      panel.hidden = true;
      stage.textContent = "";
      detail.textContent = "";
    }

    function setProgress(value, max) {
      progress.hidden = false;
      if (value === null) progress.removeAttribute("value");
      else { progress.max = max; progress.value = value; }
    }

    async function ensureSpace(size) {
      const estimate = root.navigator.storage && root.navigator.storage.estimate
        ? await root.navigator.storage.estimate().catch(() => null) : null;
      if (!estimate || !estimate.quota) return;
      const free = estimate.quota - (estimate.usage || 0);
      if (free < size * SPACE_MARGIN) {
        throw new Error(`There is not enough storage on this device (${mb(size)} needed, `
          + `${mb(Math.max(free, 0))} free). Remove a saved GeoPackage first.`);
      }
    }

    function onJob(job) {
      stage.textContent = `${job.stage}${job.state === "running" || job.state === "queued"
        ? ` (${clock(job.elapsed_s)})` : ""}`;
      detail.textContent = job.detail || "";
      setProgress(null);
    }

    async function followJob(job) {
      onJob(job);
      try {
        return await catalog.waitForJob(job.id, { onUpdate: onJob, signal: controller.signal });
      } catch (err) {
        if (err.status !== 404) throw err;
        // The server restarted and forgot the job: asking again starts (or rejoins) one.
        const again = await catalog.startBuild(selected.title, { tiles: tilesBox.checked && serverTiles });
        onJob(again);
        return catalog.waitForJob(again.id, { onUpdate: onJob, signal: controller.signal });
      }
    }

    async function run() {
      if (!selected || busy) return;
      busy = true;
      controller = new AbortController();
      start.disabled = true;
      cancel.textContent = "Cancel";
      let ok = false;
      try {
        stage.textContent = "Contacting the server…";
        setProgress(null);
        const first = await catalog.startBuild(selected.title, { tiles: tilesBox.checked && serverTiles });
        const job = await followJob(first);
        if (job.state === "failed") throw new Error(job.error || "The build failed.");
        await ensureSpace(job.size);
        stage.textContent = `Downloading ${mb(job.size)}…`;
        detail.textContent = "";
        const bytes = await catalog.download(job.url, {
          signal: controller.signal,
          onProgress: (received, total) => {
            setProgress(received, total || job.size);
            stage.textContent = `Downloading ${mb(received)} of ${mb(total || job.size)}…`;
          },
        });
        stage.textContent = "Saving on this device…";
        setProgress(null);
        await (await getFileCache()).save(job.file, bytes, { label: job.title });
        // Ask the browser not to clear these files when space runs low (best effort).
        try { await root.navigator.storage?.persist?.(); } catch (err) { /* not available */ }
        await onDownloaded({ bytes, name: job.file, label: job.title, job });
        stage.textContent = `${job.title} is on this device (${mb(bytes.length)}).`;
        detail.textContent = [...job.notes,
          job.tiles === "included" ? "Includes the offline map." : "No offline map in this file."]
          .join(" ");
        progress.hidden = true;
        start.hidden = true;
        cancel.textContent = "Close";
        ok = true;
      } catch (err) {
        if (controller.signal.aborted) {
          stage.textContent = "Cancelled. The server keeps building; ask again to pick it up.";
        } else {
          stage.textContent = `Could not get the canyon: ${err.message}`;
        }
        detail.textContent = "";
        progress.hidden = true;
      } finally {
        busy = false;
        if (!ok) {
          start.disabled = false;
          start.textContent = "Try again";
          cancel.textContent = "Close";
        }
      }
    }

    start.addEventListener("click", run);
    cancel.addEventListener("click", () => {
      if (busy && controller) controller.abort();
      else resetSelection();
    });

    root.addEventListener("online", check);
    root.addEventListener("offline", check);
    return { check };
  }

  root.catalogUI = { create };
})(typeof globalThis !== "undefined" ? globalThis : this);

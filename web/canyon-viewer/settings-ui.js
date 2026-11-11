// The settings dialog, opened from the gear on the Canyons view: units, compass, installing the
// app, saving a GeoPackage with the recorded runs and forecasts, and the offline data the page
// keeps (the saved GeoPackage and the cached app files).
(function (root) {
  "use strict";

  const formatSize = (bytes) => bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1e3))} kB`;

  // deps: { $, settings, getFileCache() -> Promise<cache>,
  //         prepareExport() -> Promise<{file, note}>, hasDatabase() -> boolean }
  function create({ $, settings, getFileCache, prepareExport, hasDatabase }) {
    const dialog = $("settings");
    const compassNote = $("compass-note");
    const fileInfo = $("saved-file");
    const appInfo = $("app-cache");

    function syncControls() {
      const current = settings.get();
      for (const radio of dialog.querySelectorAll("input[name=units]")) {
        radio.checked = radio.value === current.units;
      }
      $("set-compass").checked = current.compass;
      const imperial = current.units === "imperial";
      const shown = (c) => Math.round(imperial ? c * 1.8 + 32 : c);
      $("set-hot").value = shown(current.hotC);
      $("set-cold").value = shown(current.coldC);
      for (const unit of dialog.querySelectorAll(".temp-unit")) unit.textContent = imperial ? "°F" : "°C";
    }

    // The hot / cold limits are typed in the chosen units and kept in degrees C.
    function onLimitChange(key) {
      return (event) => {
        const current = settings.get();
        const typed = Number.parseFloat(event.target.value);
        const c = current.units === "imperial" ? (typed - 32) / 1.8 : typed;
        const next = { ...current, [key]: c };
        const valid = Number.isFinite(typed) && c >= -50 && c <= 60 && next.coldC < next.hotC;
        if (valid) settings.set({ [key]: c });
        else compassNote.textContent = "Enter a cold limit that is below the hot limit.";
        if (valid) compassNote.textContent = "";
        syncControls(); // show what was actually kept
      };
    }
    $("set-hot").addEventListener("change", onLimitChange("hotC"));
    $("set-cold").addEventListener("change", onLimitChange("coldC"));

    async function refreshOffline() {
      try {
        const info = await (await getFileCache()).info();
        fileInfo.textContent = info
          ? `${info.name} · ${formatSize(info.size)} · saved ${new Date(info.savedAt).toLocaleString()}`
          : "No GeoPackage saved.";
        $("remove-saved").disabled = !info;
      } catch (err) {
        fileInfo.textContent = `Saved GeoPackage unavailable: ${err.message}`;
        $("remove-saved").disabled = true;
      }
      let cached = false;
      try {
        cached = typeof caches !== "undefined"
          && (await caches.keys()).some((name) => name.startsWith("viewer-shell"));
      } catch (err) { /* not available */ }
      appInfo.textContent = cached
        ? "The page and its libraries are cached for offline use."
        : "Not cached yet. This needs one online load over HTTPS (or localhost).";
    }

    // iOS asks for motion-sensor permission, and only from a tap.
    async function sensorPermitted() {
      const Orientation = root.DeviceOrientationEvent;
      if (!Orientation) return { ok: false, why: "This device has no orientation sensor." };
      if (typeof Orientation.requestPermission === "function") {
        try {
          if ((await Orientation.requestPermission()) !== "granted") {
            return { ok: false, why: "Permission was denied." };
          }
        } catch (err) {
          return { ok: false, why: err.message };
        }
      }
      return { ok: true };
    }

    // ---- install ------------------------------------------------------------------
    let installPrompt = null;
    root.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      installPrompt = event;
      refreshInstall();
    });
    root.addEventListener("appinstalled", () => { installPrompt = null; refreshInstall(); });

    async function refreshInstall() {
      const state = root.install.installState({
        standalone: root.navigator.standalone === true,
        displayStandalone: Boolean(root.matchMedia && root.matchMedia("(display-mode: standalone)").matches),
        userAgent: root.navigator.userAgent,
        maxTouchPoints: root.navigator.maxTouchPoints,
        hasPrompt: Boolean(installPrompt),
      });
      $("install-note").textContent = state.text;
      // What this device reports, to tell whether the page can be placed correctly.
      const probe = root.document.createElement("div");
      probe.style.cssText = "position:fixed;visibility:hidden;"
        + "padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)";
      root.document.body.append(probe);
      const inset = root.getComputedStyle(probe);
      $("display-note").textContent = `Screen: ${root.document.documentElement.classList
        .contains("is-standalone") ? "installed app" : "browser"} · top safe area `
        + `${inset.paddingTop} · bottom ${inset.paddingBottom} · page top padding `
        + `${root.getComputedStyle(root.document.body).paddingTop} · window `
        + `${root.innerWidth}×${root.innerHeight}`;
      probe.remove();
      $("install-app").hidden = state.mode !== "prompt";
      let persisted = null;
      try { persisted = await root.navigator.storage?.persisted?.(); } catch (err) { /* unknown */ }
      $("storage-note").textContent = persisted === true
        ? "Saved data is protected from automatic clean-up."
        : persisted === false
          ? "The browser may clear saved data when space runs low; saving a GeoPackage copy is the "
            + "safe backup. Installing the app helps."
          : "";
    }
    $("install-app").addEventListener("click", async () => {
      if (!installPrompt) return;
      const prompt = installPrompt;
      installPrompt = null;
      await prompt.prompt();
      refreshInstall();
    });

    // ---- save GeoPackage ----------------------------------------------------------
    // One button. Building the file takes a few seconds, and on a phone the share sheet needs a
    // fresh tap, so there the first tap builds the file and the button then reads "Save to
    // Files…" for the second tap. Elsewhere the first tap builds the file and downloads it.
    const SAVE = "Save GeoPackage";
    const button = $("save-export");
    const exportNote = $("export-note");
    let prepared = null; // {file, note, mode: "share" | "download"}

    function clearPrepared() {
      prepared = null; // lets the browser free the file (the GeoPackage can be ~100 MB)
      button.textContent = SAVE;
      button.disabled = !hasDatabase();
    }

    async function handOver() {
      try {
        if (prepared.mode === "share") {
          const result = await root.share.shareFile(prepared.file);
          exportNote.textContent = result === "shared" ? `${prepared.file.name} shared.` : "Sharing cancelled.";
          if (result === "shared") clearPrepared();
        } else {
          root.share.downloadFile(prepared.file);
          exportNote.textContent = `${prepared.note} Download started.`;
          clearPrepared();
        }
      } catch (err) {
        prepared.mode = "download";
        button.textContent = "Download instead";
        exportNote.textContent = `Sharing failed: ${err.message}.`;
      }
    }

    button.addEventListener("click", async () => {
      if (prepared) return handOver();
      button.disabled = true;
      exportNote.textContent = "Preparing…";
      try {
        const built = await prepareExport();
        const share = root.share.prefersShareSheet() && root.share.canShareFile(built.file);
        prepared = { ...built, mode: share ? "share" : "download" };
        if (share) {
          exportNote.textContent = built.note;
          button.textContent = "Save to Files…";
        } else {
          await handOver();
        }
      } catch (err) {
        exportNote.textContent = `Could not save the GeoPackage: ${err.message}`;
      }
      button.disabled = !hasDatabase();
    });

    $("open-settings").addEventListener("click", () => {
      syncControls();
      compassNote.textContent = "";
      clearPrepared();
      exportNote.textContent = hasDatabase() ? "" : "Open a GeoPackage first.";
      refreshOffline();
      refreshInstall();
      dialog.showModal();
    });
    $("close-settings").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", clearPrepared);
    for (const radio of dialog.querySelectorAll("input[name=units]")) {
      radio.addEventListener("change", () => {
        if (radio.checked) {
          settings.set({ units: radio.value });
          syncControls();
        }
      });
    }
    $("set-compass").addEventListener("change", async (event) => {
      const box = event.target;
      compassNote.textContent = "";
      if (box.checked) {
        const result = await sensorPermitted();
        if (!result.ok) {
          box.checked = false;
          compassNote.textContent = result.why;
          settings.set({ compass: false });
          return;
        }
      }
      settings.set({ compass: box.checked });
    });
    $("remove-saved").addEventListener("click", async () => {
      await (await getFileCache()).remove();
      refreshOffline();
    });

    return { refreshOffline };
  }

  root.settingsUI = { create };
})(typeof globalThis !== "undefined" ? globalThis : this);

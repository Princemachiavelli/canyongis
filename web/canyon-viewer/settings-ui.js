// The settings dialog, opened from the gear on the Canyons view: units, compass, and the
// offline data the page keeps (the saved GeoPackage and the cached app files).
(function (root) {
  "use strict";

  const formatSize = (bytes) => bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1e3))} kB`;

  // deps: { $, settings, getFileCache() -> Promise<cache> }
  function create({ $, settings, getFileCache }) {
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
    }

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

    $("open-settings").addEventListener("click", () => {
      syncControls();
      compassNote.textContent = "";
      refreshOffline();
      dialog.showModal();
    });
    $("close-settings").addEventListener("click", () => dialog.close());
    for (const radio of dialog.querySelectorAll("input[name=units]")) {
      radio.addEventListener("change", () => { if (radio.checked) settings.set({ units: radio.value }); });
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

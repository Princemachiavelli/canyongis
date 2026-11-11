// Is the page running as an installed app, and how can it be installed? Pure logic over the
// browser's facts, so it runs in Node for tests. iOS has no install prompt: the user adds the
// page from Safari's Share menu.
(function (root) {
  "use strict";

  // env: { standalone (navigator.standalone), displayStandalone (display-mode media query),
  //        userAgent, maxTouchPoints, hasPrompt (a beforeinstallprompt event was kept) }
  function installState(env) {
    if (env.standalone || env.displayStandalone) {
      return { mode: "installed", text: "Running as an installed app." };
    }
    if (env.hasPrompt) {
      return { mode: "prompt", text: "This app can be installed on this device." };
    }
    const ua = env.userAgent || "";
    const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (env.maxTouchPoints || 0) > 1);
    if (ios) {
      const safari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
      return {
        mode: "ios",
        text: safari
          ? "To install: tap the Share button, then Add to Home Screen. The installed app keeps "
            + "its own saved GeoPackage and runs, so open your GeoPackage again inside it."
          : "To install on iOS, open this page in Safari, tap Share, then Add to Home Screen.",
      };
    }
    return {
      mode: "other",
      text: "Use your browser's Install app or Add to Home Screen option to install.",
    };
  }

  const api = { installState };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.install = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

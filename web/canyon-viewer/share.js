// Hand a file to the user: the share sheet on phones and tablets (the only dependable way to
// get a file out of an installed iOS web app, where a blob download link often does nothing),
// a normal download elsewhere. Browser only.
(function (root) {
  "use strict";

  // iPadOS reports a Mac user agent, so touch points tell tablets from desktops.
  function prefersShareSheet(nav = root.navigator) {
    if (!nav) return false;
    return /iPhone|iPad|iPod|Android/.test(nav.userAgent || "") || (nav.maxTouchPoints || 0) > 1;
  }

  function makeFile(name, data, type) {
    return new root.File([data], name, { type });
  }

  // Can this file go through the share sheet? iOS rejects media types it doesn't know, so the
  // caller may offer a second, generic type.
  function canShareFile(file, nav = root.navigator) {
    return typeof nav.share === "function" && typeof nav.canShare === "function"
      && nav.canShare({ files: [file] });
  }

  // Must be called straight from a tap (the share sheet needs a fresh user gesture), so build
  // the file first and call this from the click handler. Resolves "shared" or "cancelled";
  // rejects if sharing fails.
  async function shareFile(file, nav = root.navigator) {
    try {
      await nav.share({ files: [file], title: file.name });
      return "shared";
    } catch (err) {
      if (err && err.name === "AbortError") return "cancelled";
      throw err;
    }
  }

  function downloadFile(file) {
    const url = URL.createObjectURL(file);
    const a = root.document.createElement("a");
    a.href = url;
    a.download = file.name;
    root.document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    return "downloaded";
  }

  // Share where that is the norm and works, otherwise download. Resolves "shared",
  // "downloaded" or "cancelled".
  async function deliverFile(file) {
    if (prefersShareSheet() && canShareFile(file)) {
      try {
        return await shareFile(file);
      } catch (err) {
        console.warn("Sharing failed, downloading instead:", err);
      }
    }
    return downloadFile(file);
  }

  root.share = { prefersShareSheet, makeFile, canShareFile, shareFile, downloadFile, deliverFile };
})(typeof globalThis !== "undefined" ? globalThis : this);

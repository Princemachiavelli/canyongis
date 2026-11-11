// Client for the server's "get a canyon" API (see src/canyongis/geopackage/viewer_api.py): is the
// server reachable, search Ropewiki canyons, ask for a GeoPackage to be built, follow the job,
// and download the finished file. No DOM, so it runs in the browser (global `catalog`) and in
// Node for tests. URLs are relative to the page, so the app works under any path.
(function (root) {
  "use strict";

  const STATUS_TIMEOUT_MS = 4000;
  const SQLITE_HEADER = "SQLite format 3\0";

  class CatalogError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.name = "CatalogError";
      this.status = status;
    }
  }

  const isSqlite = (bytes) => bytes.length >= SQLITE_HEADER.length
    && [...SQLITE_HEADER].every((ch, i) => bytes[i] === ch.charCodeAt(0));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // `fetchImpl` and `base` are injectable for tests.
  function create({ fetchImpl = root.fetch && root.fetch.bind(root), base = "" } = {}) {
    async function request(path, options = {}, timeoutMs = 0) {
      const controller = new AbortController();
      const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        return await fetchImpl(base + path, { ...options, signal: options.signal || controller.signal });
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    async function json(response) {
      let body = null;
      try { body = await response.json(); } catch (err) { /* not JSON */ }
      if (!response.ok) {
        throw new CatalogError((body && body.error) || `The server answered HTTP ${response.status}`,
          response.status);
      }
      return body;
    }

    return {
      // {tiles, max_age_hours} when the server's canyon API answers, else null (offline, or a
      // server without the API). Never throws.
      async status() {
        try {
          const response = await request("api/status", {}, STATUS_TIMEOUT_MS);
          if (!response.ok) return null;
          const body = await response.json();
          return body && body.ok ? body : null;
        } catch (err) {
          return null;
        }
      },

      async search(text, { signal } = {}) {
        const response = await request(`api/search?q=${encodeURIComponent(text)}`, { signal });
        return (await json(response)).results;
      },

      async startBuild(title, { tiles = true } = {}) {
        const response = await request("api/builds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, tiles }),
        });
        return (await json(response)).job;
      },

      async getJob(id) {
        return (await json(await request(`api/builds/${id}`))).job;
      },

      // What the server holds for a canyon: each section's fingerprint and when it changed.
      // Rejects with status 404 when the server does not have the canyon.
      async manifest(title) {
        const response = await request(`api/manifest?title=${encodeURIComponent(title)}`);
        return (await json(response)).manifest;
      },

      // Ask the server to bring this device's copy up to date. `have` is {section: fingerprint}
      // from the file; `force` asks it to re-check Ropewiki even if it did in the last day.
      async startUpdate(title, have, { force = false, tiles } = {}) {
        const response = await request("api/updates", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, have, force, tiles }),
        });
        return (await json(response)).job;
      },

      // The rows of the out-of-date sections of a finished update job.
      async getDelta(jobId) {
        return (await json(await request(`api/updates/${jobId}/delta`))).delta;
      },

      // Poll until the job is done or failed, calling onUpdate with each state. Brief network
      // failures are retried; after `maxFailures` in a row the error is thrown (the server keeps
      // building, and asking again for the same canyon rejoins the job).
      async waitForJob(id, { onUpdate = () => {}, intervalMs = 2000, maxFailures = 5, signal, wait = sleep } = {}) {
        let failures = 0;
        for (;;) {
          if (signal && signal.aborted) throw new CatalogError("Cancelled");
          try {
            const job = await this.getJob(id);
            failures = 0;
            onUpdate(job);
            if (job.state === "done" || job.state === "failed") return job;
          } catch (err) {
            if (err.status === 404) throw err; // the server forgot the job (restarted)
            failures += 1;
            if (failures >= maxFailures) throw new CatalogError("Lost the connection to the server");
          }
          await wait(intervalMs);
        }
      },

      // The finished file as bytes, reporting progress as (received, total). Checks that it is
      // complete and a SQLite file before anyone stores it.
      async download(url, { onProgress = () => {}, signal } = {}) {
        const response = await request(url, { signal });
        if (!response.ok) throw new CatalogError(`Download failed (HTTP ${response.status})`, response.status);
        const total = Number(response.headers.get("content-length")) || 0;
        let bytes;
        if (response.body && response.body.getReader) {
          const reader = response.body.getReader();
          const chunks = [];
          let received = 0;
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
            received += value.length;
            onProgress(received, total);
          }
          bytes = new Uint8Array(received);
          let offset = 0;
          for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.length;
          }
        } else {
          bytes = new Uint8Array(await response.arrayBuffer());
          onProgress(bytes.length, total || bytes.length);
        }
        if (total && bytes.length !== total) {
          throw new CatalogError(`The download was cut short (${bytes.length} of ${total} bytes)`);
        }
        if (!isSqlite(bytes)) throw new CatalogError("The downloaded file is not a GeoPackage");
        return bytes;
      },
    };
  }

  const api = { create, CatalogError, isSqlite };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.catalog = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

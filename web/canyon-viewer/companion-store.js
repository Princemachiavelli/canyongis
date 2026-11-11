// Field data in IndexedDB (runs, events, fixes, and the latest weather forecast per canyon):
// it must survive a reload, so not localStorage. Canyons are keyed by their Ropewiki title and
// rappels by sequence number, because the GeoPackage's row ids change whenever a canyon is
// rebuilt.
(function (root) {
  "use strict";

  const DB_NAME = "canyongis-companion";
  const VERSION = 3;

  const wrap = (request) => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  function openDb(idb) {
    return new Promise((resolve, reject) => {
      const request = idb.open(DB_NAME, VERSION);
      request.onupgradeneeded = (event) => {
        const db = request.result;
        if (event.oldVersion < 1) {
          const runs = db.createObjectStore("runs", { keyPath: "id" });
          runs.createIndex("canyonId", "canyonId");
          const events = db.createObjectStore("events", { keyPath: "id", autoIncrement: true });
          events.createIndex("runId", "runId");
          events.createIndex("canyonId", "canyonId");
          const fixes = db.createObjectStore("fixes", { keyPath: "id", autoIncrement: true });
          fixes.createIndex("runId", "runId");
        }
        if (event.oldVersion < 3) {
          // Applied updates of downloaded canyons (see gpkg-update.js): {id, title, section, ...}.
          db.createObjectStore("patches", { keyPath: "id" });
        }
        if (event.oldVersion < 2) {
          // One forecast per canyon: {canyonId: title, ...forecast record}.
          db.createObjectStore("forecasts", { keyPath: "canyonId" });
        }
      };
      request.onsuccess = () => {
        // Let a newer version of the page (another tab or a reload) upgrade the database.
        request.result.onversionchange = () => request.result.close();
        resolve(request.result);
      };
      request.onerror = () => reject(request.error);
    });
  }

  // `lib` is the companion logic module (event helpers). Returns the store API.
  async function openStore(lib, idb = root.indexedDB) {
    if (!idb) throw new Error("This browser has no IndexedDB, so runs cannot be saved");
    const db = await openDb(idb);

    // Run `work(stores)` in one transaction and resolve with its result once committed.
    function inTx(names, mode, work) {
      return new Promise((resolve, reject) => {
        const tx = db.transaction(names, mode);
        const stores = Object.fromEntries(names.map((n) => [n, tx.objectStore(n)]));
        let result;
        Promise.resolve(work(stores)).then((r) => { result = r; }, reject);
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    }

    const byIndex = (store, index, key) => wrap(store.index(index).getAll(key));

    return {
      async startRun(canyonId, fix) {
        const run = {
          id: crypto.randomUUID(), canyonId, startedAt: Date.now(), endedAt: null, nextIndex: 0,
        };
        await inTx(["runs", "events"], "readwrite", (s) => {
          s.runs.add(run);
          s.events.add(lib.makeEvent(run, "start", fix, null));
        });
        return run;
      },

      async runsFor(canyonId) {
        const runs = await inTx(["runs"], "readonly", (s) => byIndex(s.runs, "canyonId", canyonId));
        return runs.sort((a, b) => b.startedAt - a.startedAt);
      },

      // Move to another rappel (next / back / jump), recording where and when.
      async moveTo(run, type, nextIndex, from, fix) {
        run.nextIndex = nextIndex;
        await inTx(["runs", "events"], "readwrite", (s) => {
          s.runs.put(run);
          s.events.add(lib.makeEvent(run, type, fix, from));
        });
      },

      // Save the user's position as the true location of a rappel.
      async markRappelLocation(run, rappel, fix) {
        await inTx(["events"], "readwrite", (s) => {
          s.events.add(lib.makeEvent(run, "mark", fix, rappel));
        });
      },

      async finishRun(run, fix) {
        run.endedAt = Date.now();
        await inTx(["runs", "events"], "readwrite", (s) => {
          s.runs.put(run);
          s.events.add(lib.makeEvent(run, "finish", fix, null));
        });
      },

      // Remove a run with its events (including positions saved during it) and GPS fixes.
      async deleteRun(runId) {
        await inTx(["runs", "events", "fixes"], "readwrite", async (s) => {
          s.runs.delete(runId);
          for (const name of ["events", "fixes"]) {
            for (const key of await wrap(s[name].index("runId").getAllKeys(runId))) {
              s[name].delete(key);
            }
          }
        });
      },

      async addFix(runId, fix) {
        await inTx(["fixes"], "readwrite", (s) => {
          s.fixes.add({ runId, lat: fix.lat, lon: fix.lon, acc: fix.acc, t: fix.t });
        });
      },

      async recordedPositions(canyonId) {
        const events = await inTx(["events"], "readonly", (s) => byIndex(s.events, "canyonId", canyonId));
        return lib.recordedFromMarks(events);
      },

      // Everything recorded on this device, for writing into a GeoPackage.
      async allRecords() {
        return inTx(["runs", "events", "fixes", "forecasts"], "readonly", async (s) => {
          const [runs, events, fixes, forecasts] = await Promise.all(
            ["runs", "events", "fixes", "forecasts"].map((name) => wrap(s[name].getAll())));
          return {
            runs, events, fixes,
            forecasts: Object.fromEntries(forecasts.map(({ canyonId, ...record }) => [canyonId, record])),
          };
        });
      },

      async savePatches(patches) {
        await inTx(["patches"], "readwrite", (s) => { for (const p of patches) s.patches.put(p); });
      },
      async allPatches() {
        return inTx(["patches"], "readonly", (s) => wrap(s.patches.getAll()));
      },
      async deletePatches(ids) {
        await inTx(["patches"], "readwrite", (s) => { for (const id of ids) s.patches.delete(id); });
      },

      // The forecast fetched on this device for a canyon, or null.
      async getForecast(canyonId) {
        const row = await inTx(["forecasts"], "readonly", (s) => wrap(s.forecasts.get(canyonId)));
        if (!row) return null;
        const { canyonId: _id, ...record } = row;
        return record;
      },

      async saveForecast(canyonId, record) {
        await inTx(["forecasts"], "readwrite", (s) => { s.forecasts.put({ ...record, canyonId }); });
      },

      async exportRun(runId) {
        const [run, events, fixes] = await inTx(["runs", "events", "fixes"], "readonly", (s) =>
          Promise.all([wrap(s.runs.get(runId)), byIndex(s.events, "runId", runId),
                       byIndex(s.fixes, "runId", runId)]));
        return lib.exportDocument(run, events, fixes);
      },
    };
  }

  const api = { openStore };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.companionStore = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

// The GeoPackages kept on this device (IndexedDB), so the page can open them without the file
// picker or a network (a file otherwise exists only in memory). Several files are kept, each as a
// small metadata record apart from its bytes so lists can be shown without loading 100 MB, plus
// a pointer to the one opened last (what a reload reopens).
//
// Keys in the "files" store: `meta:<name>` {name, size, savedAt, label}, `bytes:<name>` {bytes},
// `last` {name}. Older versions of the page kept a single file under `last:meta` / `last:bytes`;
// it is moved into the new layout the first time the cache is opened.
(function (root) {
  "use strict";

  const DB_NAME = "canyongis-viewer";
  const STORE = "files";
  const META = (name) => `meta:${name}`;
  const BYTES = (name) => `bytes:${name}`;
  const LAST = "last";
  const LEGACY_META = "last:meta";
  const LEGACY_BYTES = "last:bytes";

  const wrap = (request) => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const done = (tx) => new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("The save was cancelled"));
  });

  async function migrate(db) {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const [meta, data, pointer] = await Promise.all(
      [LEGACY_META, LEGACY_BYTES, LAST].map((key) => wrap(store.get(key))));
    if (meta && data) {
      store.put({ id: META(meta.name), name: meta.name, size: meta.size, savedAt: meta.savedAt });
      store.put({ id: BYTES(meta.name), bytes: data.bytes });
      if (!pointer) store.put({ id: LAST, name: meta.name });
    }
    if (meta || data) {
      store.delete(LEGACY_META);
      store.delete(LEGACY_BYTES);
    }
    await done(tx);
  }

  async function openFileCache(idb = root.indexedDB) {
    if (!idb) throw new Error("This browser has no IndexedDB");
    const db = await new Promise((resolve, reject) => {
      const request = idb.open(DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await migrate(db);

    const read = (work) => {
      const tx = db.transaction(STORE, "readonly");
      return work(tx.objectStore(STORE));
    };
    const strip = ({ id, ...meta }) => meta; // eslint-disable-line no-unused-vars

    async function list() {
      const metas = await read(async (store) => {
        const keys = (await wrap(store.getAllKeys())).filter((k) => String(k).startsWith("meta:"));
        return Promise.all(keys.map((key) => wrap(store.get(key))));
      });
      return metas.filter(Boolean).map(strip).sort((a, b) => b.savedAt - a.savedAt);
    }
    async function lastName() {
      const pointer = await read((store) => wrap(store.get(LAST)));
      return pointer ? pointer.name : null;
    }

    return {
      // Keep a file (replacing one of the same name) and make it the one reopened on reload. All
      // or nothing: if the browser refuses the write (no space), earlier files are untouched
      // and this rejects.
      // `canyons` is a short summary of what the file holds ([{title, region, ...}]), so the
      // page can list its canyons without opening the file.
      async save(name, bytes, { label = null, canyons = null } = {}) {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        store.put({ id: BYTES(name), bytes });
        store.put({ id: META(name), name, size: bytes.length, savedAt: Date.now(), label, canyons });
        store.put({ id: LAST, name });
        await done(tx);
      },
      // Add to a saved file's metadata (such as `canyons`, for files saved before it existed).
      // Does nothing if the file is not saved.
      async setMeta(name, patch) {
        const meta = await read((store) => wrap(store.get(META(name))));
        if (!meta) return false;
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).put({ ...meta, ...patch, id: meta.id, name: meta.name });
        await done(tx);
        return true;
      },
      list,
      // {name, size, savedAt, label} of the named file (default: the last opened), or null.
      async info(name) {
        const target = name || (await lastName());
        if (!target) return null;
        const meta = await read((store) => wrap(store.get(META(target))));
        return meta ? strip(meta) : null;
      },
      // {name, size, savedAt, label, bytes} or null.
      async load(name) {
        const target = name || (await lastName());
        if (!target) return null;
        const [meta, data] = await read((store) =>
          Promise.all([wrap(store.get(META(target))), wrap(store.get(BYTES(target)))]));
        return meta && data ? { ...strip(meta), bytes: data.bytes } : null;
      },
      // Make `name` the file reopened on reload.
      async setLast(name) {
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).put({ id: LAST, name });
        await done(tx);
      },
      async remove(name) {
        const target = name || (await lastName());
        if (!target) return;
        const wasLast = (await lastName()) === target;
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(META(target));
        tx.objectStore(STORE).delete(BYTES(target));
        await done(tx);
        if (wasLast) {
          const [newest] = await list();
          const tx2 = db.transaction(STORE, "readwrite");
          if (newest) tx2.objectStore(STORE).put({ id: LAST, name: newest.name });
          else tx2.objectStore(STORE).delete(LAST);
          await done(tx2);
        }
      },
    };
  }

  const api = { openFileCache };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.filecache = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

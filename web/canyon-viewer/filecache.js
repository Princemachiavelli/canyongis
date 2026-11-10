// Remembers the last-opened GeoPackage in IndexedDB so the page can reopen it without the file
// picker or a network (the file otherwise exists only in memory). One saved file; its metadata
// is kept apart from the bytes so the settings menu can show it without loading 40 MB.
(function (root) {
  "use strict";

  const DB_NAME = "canyongis-viewer";
  const STORE = "files";
  const META = "last:meta";
  const BYTES = "last:bytes";

  const wrap = (request) => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  async function openFileCache(idb = root.indexedDB) {
    if (!idb) throw new Error("This browser has no IndexedDB");
    const db = await new Promise((resolve, reject) => {
      const request = idb.open(DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const done = (tx) => new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    return {
      async save(name, bytes) {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        store.put({ id: BYTES, bytes });
        store.put({ id: META, name, size: bytes.length, savedAt: Date.now() });
        await done(tx);
      },
      // {name, size, savedAt} or null.
      async info() {
        return (await wrap(db.transaction(STORE).objectStore(STORE).get(META))) || null;
      },
      // {name, size, savedAt, bytes} or null.
      async load() {
        const store = db.transaction(STORE).objectStore(STORE);
        const [meta, data] = await Promise.all([wrap(store.get(META)), wrap(store.get(BYTES))]);
        return meta && data ? { ...meta, bytes: data.bytes } : null;
      },
      async remove() {
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(META);
        tx.objectStore(STORE).delete(BYTES);
        await done(tx);
      },
    };
  }

  const api = { openFileCache };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.filecache = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

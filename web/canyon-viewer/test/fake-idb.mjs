// A tiny in-memory IndexedDB for tests: one database, object stores of {id, ...} records,
// transactions that apply their writes all at once (or not at all when `failWrites` is set,
// like a browser out of storage), and a log of the keys read.
export function makeIdb(initial = {}) {
  const stores = { files: new Map(Object.entries(initial)) };
  const idb = { stores, reads: [], failWrites: false, created: false };
  idb.open = () => {
    const request = {};
    setTimeout(() => {
      const db = {
        createObjectStore() { idb.created = true; },
        transaction(storeName, mode) {
          const tx = { pending: 0, writes: [], done: false };
          const finishSoon = () => setTimeout(() => {
            if (tx.pending > 0 || tx.done) return;
            tx.done = true;
            if (tx.writes.length && idb.failWrites) {
              tx.error = Object.assign(new Error("Quota exceeded"), { name: "QuotaExceededError" });
              tx.onabort?.();
              return;
            }
            for (const [op, key, value] of tx.writes) {
              if (op === "put") stores[storeName].set(key, value);
              else stores[storeName].delete(key);
            }
            tx.oncomplete?.();
          }, 0);
          const issue = (work, write) => {
            const req = {};
            tx.pending += 1;
            setTimeout(() => {
              if (write && mode !== "readwrite") throw new Error("write in a readonly transaction");
              req.result = work();
              req.onsuccess?.();
              tx.pending -= 1;
              finishSoon();
            }, 0);
            return req;
          };
          finishSoon();
          return {
            objectStore: () => ({
              get: (key) => issue(() => { idb.reads.push(key); return stores[storeName].get(key); }),
              getAllKeys: () => issue(() => [...stores[storeName].keys()]),
              put: (value) => issue(() => { tx.writes.push(["put", value.id, value]); }, true),
              delete: (key) => issue(() => { tx.writes.push(["delete", key]); }, true),
            }),
            set oncomplete(fn) { tx.oncomplete = fn; },
            set onerror(fn) { tx.onerror = fn; },
            set onabort(fn) { tx.onabort = fn; },
            get error() { return tx.error; },
          };
        },
      };
      request.result = db;
      if (!idb.created && stores.files.size === 0) request.onupgradeneeded?.();
      request.onsuccess?.();
    }, 0);
    return request;
  };
  return idb;
}

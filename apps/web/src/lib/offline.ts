// Workbox keeps a small database of when each runtime cache entry was stored, so it can expire
// old ones. Deleting the API caches on sign-out leaves those rows behind; they hold the cached
// addresses, such as /api/v1/queue?doctor_user_id=<id>.
const BOOKKEEPING_DB = "workbox-expiration";
const BOOKKEEPING_STORE = "cache-entries";
// Row ids are "<cache name>|<url>". Built only when needed, so importing this module works where
// IndexedDB does not exist.
const apiEntries = (): IDBKeyRange => IDBKeyRange.bound("hs-api", "hs-api￿");

/** Removes Workbox's bookkeeping rows for the hs-api caches. Never creates the database. */
export async function clearCacheBookkeeping(): Promise<void> {
  if (typeof indexedDB === "undefined") {
    return;
  }
  if ("databases" in indexedDB) {
    const dbs = await indexedDB.databases();
    if (!dbs.some((d) => d.name === BOOKKEEPING_DB)) {
      return;
    }
  }
  await new Promise<void>((resolve) => {
    const req = indexedDB.open(BOOKKEEPING_DB);
    // The database did not exist after all: do not create it.
    req.onupgradeneeded = () => req.transaction?.abort();
    req.onerror = () => resolve();
    req.onblocked = () => resolve();
    req.onsuccess = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(BOOKKEEPING_STORE)) {
        db.close();
        resolve();
        return;
      }
      const tx = db.transaction(BOOKKEEPING_STORE, "readwrite");
      tx.objectStore(BOOKKEEPING_STORE).delete(apiEntries());
      const done = () => {
        db.close();
        resolve();
      };
      tx.oncomplete = done;
      tx.onerror = done;
      tx.onabort = done;
    };
  });
}

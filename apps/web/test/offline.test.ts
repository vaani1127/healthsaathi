import "fake-indexeddb/auto";

import { afterEach, describe, expect, it } from "vitest";

import { clearCacheBookkeeping } from "@/lib/offline";

function open(name: string, setup?: (db: IDBDatabase) => void): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = () => setup?.(req.result);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function keys(): Promise<string[]> {
  const db = await open("workbox-expiration");
  const all = await new Promise<string[]>((resolve) => {
    const req = db.transaction("cache-entries").objectStore("cache-entries").getAllKeys();
    req.onsuccess = () => resolve(req.result.map(String));
  });
  db.close();
  return all;
}

afterEach(async () => {
  await new Promise((resolve) => {
    const req = indexedDB.deleteDatabase("workbox-expiration");
    req.onsuccess = resolve;
    req.onerror = resolve;
  });
});

describe("clearCacheBookkeeping", () => {
  it("removes the rows of the API caches and keeps the others", async () => {
    const db = await open("workbox-expiration", (d) => d.createObjectStore("cache-entries", { keyPath: "id" }));
    const tx = db.transaction("cache-entries", "readwrite");
    for (const id of ["hs-api-queue|http://localhost/api/v1/queue?doctor_user_id=1", "hs-api-queue|http://localhost/api/v1/queue", "hs-config|http://localhost/config.js"]) {
      tx.objectStore("cache-entries").put({ id, timestamp: 1 });
    }
    await new Promise((resolve) => (tx.oncomplete = resolve));
    db.close();

    await clearCacheBookkeeping();
    expect(await keys()).toEqual(["hs-config|http://localhost/config.js"]);
  });

  it("does not create the database when there is none", async () => {
    await clearCacheBookkeeping();
    const names = (await indexedDB.databases()).map((d) => d.name);
    expect(names).not.toContain("workbox-expiration");
  });
});

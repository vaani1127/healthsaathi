// No fake-indexeddb here: the test environment has no IndexedDB, like an old or locked-down
// browser. Importing the module (the auth store does on every page) must not throw.
import { describe, expect, it } from "vitest";

describe("offline helpers without IndexedDB", () => {
  it("imports and does nothing", async () => {
    expect(typeof indexedDB).toBe("undefined");
    expect(typeof IDBKeyRange).toBe("undefined");
    const { clearCacheBookkeeping } = await import("@/lib/offline");
    await expect(clearCacheBookkeeping()).resolves.toBeUndefined();
  });
});

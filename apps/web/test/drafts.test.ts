import "fake-indexeddb/auto";

import { beforeEach, describe, expect, it } from "vitest";

import { deleteDraft, dropAllDrafts, forgetSessionKey, listDrafts, saveDraft } from "@/lib/drafts";

const draft = (id: string) => ({
  id,
  patientId: "p1",
  values: { pulse: 72 },
  createdAt: new Date(2026, 9, 8, 10, Number(id)).toISOString(),
});

describe("vitals drafts", () => {
  beforeEach(async () => {
    await dropAllDrafts();
  });

  it("stores and reads back drafts in order", async () => {
    await saveDraft(draft("2"));
    await saveDraft(draft("1"));
    expect((await listDrafts()).map((d) => d.id)).toEqual(["1", "2"]);
    await deleteDraft("1");
    expect((await listDrafts()).map((d) => d.id)).toEqual(["2"]);
  });

  it("drafts from an earlier session are unreadable and removed", async () => {
    await saveDraft(draft("3"));
    forgetSessionKey();
    expect(await listDrafts()).toEqual([]);
    forgetSessionKey();
    expect(await listDrafts()).toEqual([]);
  });

  it("stores ciphertext, not the values", async () => {
    await saveDraft(draft("4"));
    const raw = await new Promise<unknown[]>((resolve) => {
      const req = indexedDB.open("hs-drafts", 1);
      req.onsuccess = () => {
        const get = req.result.transaction("vitals").objectStore("vitals").getAll();
        get.onsuccess = () => resolve(get.result);
      };
    });
    expect(JSON.stringify(raw)).not.toContain("pulse");
  });
});

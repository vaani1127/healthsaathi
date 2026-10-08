/**
 * Unsent vitals drafts for when the network drops (SPEC 4).
 *
 * Drafts are stored in IndexedDB encrypted with AES-GCM. The key is created in memory for this
 * session and cannot be exported, so after a reload or sign out the stored drafts can no longer
 * be read and are deleted.
 */

const DB_NAME = "hs-drafts";
const STORE = "vitals";

export interface VitalsDraft {
  id: string;
  patientId: string;
  values: Record<string, unknown>;
  createdAt: string;
}

interface StoredDraft {
  id: string;
  iv: Uint8Array;
  data: ArrayBuffer;
}

let sessionKey: CryptoKey | null = null;

async function key(): Promise<CryptoKey> {
  if (!sessionKey) {
    sessionKey = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, [
      "encrypt",
      "decrypt",
    ]);
  }
  return sessionKey;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const req = fn(db.transaction(STORE, mode).objectStore(STORE));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

export async function saveDraft(draft: VitalsDraft): Promise<void> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    await key(),
    new TextEncoder().encode(JSON.stringify(draft)),
  );
  await withStore("readwrite", (s) => s.put({ id: draft.id, iv, data } satisfies StoredDraft));
}

/** Drafts readable in this session. Drafts from an earlier session are deleted. */
export async function listDrafts(): Promise<VitalsDraft[]> {
  const rows = await withStore<StoredDraft[]>("readonly", (s) => s.getAll() as IDBRequest<StoredDraft[]>);
  const drafts: VitalsDraft[] = [];
  for (const row of rows) {
    try {
      const plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: new Uint8Array(row.iv) },
        await key(),
        row.data,
      );
      drafts.push(JSON.parse(new TextDecoder().decode(plain)) as VitalsDraft);
    } catch {
      await deleteDraft(row.id);
    }
  }
  return drafts.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}

export async function deleteDraft(id: string): Promise<void> {
  await withStore("readwrite", (s) => s.delete(id));
}

export async function dropAllDrafts(): Promise<void> {
  sessionKey = null;
  await withStore("readwrite", (s) => s.clear());
}

/** Forget the key only, as a reload would. Used in tests. */
export function forgetSessionKey(): void {
  sessionKey = null;
}

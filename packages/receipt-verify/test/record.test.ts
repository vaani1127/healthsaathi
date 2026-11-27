import { encodeAbiParameters, encodeEventTopics, type Hex, type Log } from "viem";
import { describe, expect, it } from "vitest";

import {
  auditAnchorAbi,
  checkAnchor,
  checkRecordAnchor,
  commitment,
  isRecordCheckResponse,
  type Json,
  type RecordCheckResponse,
  type RecordVersionProof,
  verifyRecordCheck,
} from "../src";
import vectors from "./record-vectors.json";

const base = vectors.record_check as unknown as RecordCheckResponse;
const fresh = (): RecordCheckResponse => structuredClone(base);

function at<T>(items: T[], i: number): T {
  const item = items[i];
  if (item === undefined) {
    throw new Error(`no item ${i}`);
  }
  return item;
}
const ANCHOR = "0x00000000000000000000000000000000000a0c40" as Hex;

describe("commitment vectors", () => {
  it.each(vectors.commitment)("salt $salt", ({ salt, content, commitment: expected }) => {
    expect(commitment(salt, content as { [key: string]: Json })).toBe(expected);
  });

  it("rejects a salt that is not 32 bytes", () => {
    expect(() => commitment("00", {})).toThrow();
  });
});

describe("verifyRecordCheck", () => {
  it("verifies the response the API format produces", () => {
    const check = verifyRecordCheck(fresh());
    expect(check.verdict).toBe("verified");
    expect(check.clinic).toBe(true);
    expect(check.versions.map((v) => [v.version, v.current, v.commitment, v.ok])).toEqual([
      [1, false, null, true],
      [2, true, true, true],
    ]);
    expect(isRecordCheckResponse(base)).toBe(true);
  });

  it("fails when one byte of the content changes", () => {
    const r = fresh();
    const content = at(r.versions, 1).content as { values: { hb: { unit: string } } };
    content.values.hb.unit = "g/dM";
    const check = verifyRecordCheck(r);
    expect(at(check.versions, 1).commitment).toBe(false);
    expect(check.verdict).toBe("failed");
  });

  it("fails with a wrong salt", () => {
    const r = fresh();
    const salt = at(r.versions, 1).salt ?? "";
    at(r.versions, 1).salt = (salt[0] === "0" ? "1" : "0") + salt.slice(1);
    expect(verifyRecordCheck(r).verdict).toBe("failed");
  });

  it("fails when a leaf is swapped for another version's leaf", () => {
    const r = fresh();
    const older = at(r.versions, 0);
    const newer: RecordVersionProof = at(r.versions, 1);
    newer.payload = structuredClone(older.payload);
    newer.payload_hash = older.payload_hash;
    newer.leaf_hash = older.leaf_hash;
    newer.leaf_index = older.leaf_index;
    newer.proof = structuredClone(older.proof);
    const check = verifyRecordCheck(r);
    expect(at(check.versions, 1).leaf).toBe(false);
    expect(check.verdict).toBe("failed");
  });

  it("fails when the leaf names another commitment, even if it hashes correctly", () => {
    const r = fresh();
    at(r.versions, 1).commitment = "00".repeat(32);
    expect(verifyRecordCheck(r).verdict).toBe("failed");
  });

  it("fails when the proof does not reach the signed root", () => {
    const r = fresh();
    at(r.versions, 0).leaf_index = 3;
    expect(at(verifyRecordCheck(r).versions, 0).inclusion).toBe(false);
  });

  it("fails when the content of the newest version is missing (row gone)", () => {
    const r = fresh();
    at(r.versions, 1).content = null;
    expect(verifyRecordCheck(r).verdict).toBe("failed");
  });

  it("is pending when a version is not in a signed checkpoint yet", () => {
    const r = fresh();
    at(r.versions, 1).proof = null;
    at(r.versions, 1).sth = null;
    at(r.versions, 1).signature = null;
    expect(verifyRecordCheck(r).verdict).toBe("pending");
  });

  it("reports a record without versions and backfilled versions", () => {
    expect(verifyRecordCheck({ ...fresh(), versions: [] }).verdict).toBe("not_committed");
    const r = fresh();
    at(r.versions, 0).backfilled = true;
    const check = verifyRecordCheck(r);
    expect(check.backfilled).toBe(true);
    expect(check.coveredSince).toBe(base.covered_since);
  });
});

function client(logs: Log[], latest: [bigint, Hex] = [0n, ("0x" + "00".repeat(32)) as Hex]) {
  return {
    readContract: async () => latest,
    getTransactionReceipt: async () => ({ logs, blockNumber: 7n }),
  } as unknown as Parameters<typeof checkAnchor>[0];
}

function anchoredLog(root: string): Log {
  const topics = encodeEventTopics({
    abi: auditAnchorAbi,
    eventName: "Anchored",
    args: { clinicId: base.clinic_id_bytes32 as Hex },
  });
  const sth = at(base.versions, 1).sth as { tree_size: number };
  const data = encodeAbiParameters(
    [{ type: "uint64" }, { type: "bytes32" }, { type: "bytes32" }, { type: "uint256" }],
    [BigInt(sth.tree_size), root as Hex, vectors.sth_digest as Hex, 1_795_000_000n],
  );
  return { address: ANCHOR, topics, data } as unknown as Log;
}

describe("checkRecordAnchor", () => {
  const root = "0x" + (at(base.versions, 1).sth as { root_hex: string }).root_hex;

  it("finds the Anchored event of the tree head", async () => {
    const result = await checkRecordAnchor(client([anchoredLog(root)]), fresh(), { auditAnchor: ANCHOR });
    expect(result?.status).toBe("anchored");
    expect(result?.txHash).toBe(at(at(base.versions, 1).anchors, 0).tx_ref);
  });

  it("reports a different root on chain as a mismatch", async () => {
    const result = await checkRecordAnchor(client([anchoredLog("0x" + "cd".repeat(32))]), fresh(), {
      auditAnchor: ANCHOR,
    });
    expect(result?.status).toBe("mismatch");
  });

  it("has nothing to check without a proof", async () => {
    const r = fresh();
    for (const v of r.versions) {
      v.sth = null;
    }
    expect(await checkRecordAnchor(client([]), r, { auditAnchor: ANCHOR })).toBeNull();
  });
});

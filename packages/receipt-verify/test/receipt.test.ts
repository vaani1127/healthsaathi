import { encodeAbiParameters, encodeEventTopics, type Hex, type Log } from "viem";
import { describe, expect, it } from "vitest";

import {
  auditAnchorAbi,
  checkAnchor,
  publicKeyHash,
  type Receipt,
  sthDigestHex,
  verifyReceipt,
} from "../src";
import vectors from "./vectors/ledger.json";

const receipt = vectors.api_receipt as unknown as Receipt & { sth_digest: string };
const ANCHOR = "0x00000000000000000000000000000000000a0c40" as Hex;
const REGISTRY = "0x00000000000000000000000000000000000e6157" as Hex;
const TX = "0x" + "ab".repeat(32);

describe("verifyReceipt", () => {
  it("accepts the receipt the API format produces", () => {
    expect(verifyReceipt(receipt)).toEqual({
      payload: true,
      clinic: true,
      inclusion: true,
      signature: true,
      ok: true,
    });
    expect(sthDigestHex(receipt.sth)).toBe("0x" + receipt.sth_digest);
  });

  it("rejects a changed payload", () => {
    const changed = { ...receipt, payload: { ...receipt.payload, role: "nurse" } };
    const check = verifyReceipt(changed);
    expect(check.payload).toBe(false);
    expect(check.ok).toBe(false);
  });

  it("rejects a head for another clinic or a swapped key", () => {
    expect(verifyReceipt({ ...receipt, clinic_id: "someone-else" }).ok).toBe(false);
    const other = { ...receipt, public_key: "11".repeat(32) };
    expect(verifyReceipt(other).signature).toBe(false);
    expect(verifyReceipt({ ...receipt, payload_hash: "zz" }).payload).toBe(false);
  });
});

function anchoredLog(root: string, digest: string, treeSize = receipt.sth.tree_size): Log {
  const topics = encodeEventTopics({
    abi: auditAnchorAbi,
    eventName: "Anchored",
    args: { clinicId: receipt.clinic_id_bytes32 as Hex },
  });
  const data = encodeAbiParameters(
    [{ type: "uint64" }, { type: "bytes32" }, { type: "bytes32" }, { type: "uint256" }],
    [BigInt(treeSize), root as Hex, digest as Hex, 1_790_000_000n],
  );
  return { address: ANCHOR, topics, data } as unknown as Log;
}

interface Fake {
  logs?: Log[];
  latest?: [bigint, Hex];
  keyHash?: Hex;
}

function client(fake: Fake) {
  return {
    readContract: async ({ functionName }: { functionName: string }) => {
      if (functionName === "signerKeyHash") {
        return fake.keyHash ?? ("0x" + "00".repeat(32));
      }
      return fake.latest ?? [0n, "0x" + "00".repeat(32)];
    },
    getTransactionReceipt: async () => ({ logs: fake.logs ?? [], blockNumber: 42n }),
  } as unknown as Parameters<typeof checkAnchor>[0];
}

const root = "0x" + receipt.sth.root_hex;
const digest = "0x" + receipt.sth_digest;
const withTx = {
  ...receipt,
  anchors: [{ backend: "amoy", status: "confirmed", tx_ref: TX, block_ref: "42", link: null }],
};

describe("checkAnchor", () => {
  it("finds the Anchored event in the named transaction", async () => {
    const result = await checkAnchor(
      client({ logs: [anchoredLog(root, digest)], keyHash: publicKeyHash(receipt.public_key) as Hex }),
      withTx,
      { auditAnchor: ANCHOR, clinicRegistry: REGISTRY },
    );
    expect(result).toEqual({ status: "anchored", keyRegistered: true, txHash: TX, blockNumber: 42n });
  });

  it("reports a different root for the same size as a mismatch", async () => {
    const result = await checkAnchor(
      client({ logs: [anchoredLog("0x" + "cd".repeat(32), digest)] }),
      withTx,
      { auditAnchor: ANCHOR, clinicRegistry: REGISTRY },
    );
    expect(result.status).toBe("mismatch");
    expect(result.keyRegistered).toBe(false);
  });

  it("ignores logs from other contracts and falls back to the latest head", async () => {
    const foreign = { ...anchoredLog(root, digest), address: REGISTRY } as Log;
    const result = await checkAnchor(
      client({ logs: [foreign], latest: [BigInt(receipt.sth.tree_size), root as Hex] }),
      withTx,
      { auditAnchor: ANCHOR },
    );
    expect(result).toEqual({ status: "anchored", keyRegistered: null, txHash: null, blockNumber: null });
  });

  it("says not anchored when the chain is at another size", async () => {
    const result = await checkAnchor(client({ latest: [2n, root as Hex] }), receipt, {
      auditAnchor: ANCHOR,
    });
    expect(result.status).toBe("not_anchored");
    const wrong = await checkAnchor(
      client({ latest: [BigInt(receipt.sth.tree_size), ("0x" + "00".repeat(32)) as Hex] }),
      receipt,
      { auditAnchor: ANCHOR },
    );
    expect(wrong.status).toBe("mismatch");
  });
});

import { describe, expect, it } from "vitest";

import {
  bytesToHex,
  canonicalize,
  hexToBytes,
  type Json,
  keyId,
  leafHash,
  nodeHash,
  type ReceiptProof,
  rootMatches,
  signingBytes,
  type TreeHead,
  treeHeadDigest,
  verifyConsistency,
  verifyInclusion,
  verifyReceiptProof,
  verifyTreeHead,
} from "../src";
import vectors from "./vectors/ledger.json";

const hex = (items: string[]) => items.map(hexToBytes);

function rootOf(leaves: Uint8Array[]): Uint8Array {
  // Naive RFC 6962 MTH, used only to cross-check the committed roots.
  if (leaves.length === 0) {
    return hexToBytes("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  }
  if (leaves.length === 1) {
    return leaves[0] as Uint8Array;
  }
  let k = 1;
  while (k * 2 < leaves.length) {
    k *= 2;
  }
  return nodeHash(rootOf(leaves.slice(0, k)), rootOf(leaves.slice(k)));
}

describe("JCS vectors", () => {
  it.each(vectors.jcs)("canonicalizes $input", ({ input, canonical }) => {
    expect(canonicalize(JSON.parse(input) as Json)).toBe(canonical);
  });

  it("rejects non-finite numbers and lone surrogates", () => {
    expect(() => canonicalize(Number.NaN)).toThrow();
    expect(() => canonicalize("\ud800")).toThrow();
  });
});

describe("Merkle vectors", () => {
  const leafHashes = hex(vectors.merkle.leaf_hashes);

  it("computes the same leaf hashes", () => {
    const data = hex(vectors.merkle.leaf_data);
    expect(data.map((d) => bytesToHex(leafHash(d)))).toEqual(vectors.merkle.leaf_hashes);
  });

  it.each(vectors.merkle.roots)("root of size $size matches", ({ size, root }) => {
    expect(bytesToHex(rootOf(leafHashes.slice(0, size)))).toBe(root);
  });

  it.each(vectors.merkle.inclusion)(
    "inclusion index $index size $size valid=$valid",
    ({ index, size, leaf_hash, proof, root, valid }) => {
      expect(
        verifyInclusion(hexToBytes(leaf_hash), index, size, hex(proof), hexToBytes(root)),
      ).toBe(valid);
    },
  );

  it.each(vectors.merkle.consistency)(
    "consistency $old_size -> $new_size valid=$valid",
    ({ old_size, new_size, old_root, new_root, proof, valid }) => {
      expect(
        verifyConsistency(old_size, new_size, hexToBytes(old_root), hexToBytes(new_root), hex(proof)),
      ).toBe(valid);
    },
  );

  it("rejects a flipped bit anywhere in a proof", () => {
    const c = vectors.merkle.inclusion.find((x) => x.size === 13 && x.valid);
    expect(c).toBeDefined();
    const { index, size, leaf_hash, proof, root } = c as (typeof vectors.merkle.inclusion)[0];
    proof.forEach((_, j) => {
      const bad = hex(proof);
      (bad[j] as Uint8Array)[0] = ((bad[j] as Uint8Array)[0] as number) ^ 1;
      expect(verifyInclusion(hexToBytes(leaf_hash), index, size, bad, hexToBytes(root))).toBe(false);
    });
    expect(verifyInclusion(hexToBytes(leaf_hash), -1, size, hex(proof), hexToBytes(root))).toBe(
      false,
    );
  });
});

describe("Signed tree head vectors", () => {
  const publicKey = hexToBytes(vectors.sth.public_key);

  it("derives the same key id", () => {
    expect(keyId(publicKey)).toBe(vectors.sth.key_id);
  });

  it.each(vectors.sth.heads)("verifies head valid=$valid", (c) => {
    const head = c.head as TreeHead;
    expect(verifyTreeHead(head, hexToBytes(c.signature), publicKey)).toBe(c.valid);
    if (c.valid) {
      expect(new TextDecoder().decode(signingBytes(head))).toBe(c.signing_bytes);
      expect(bytesToHex(treeHeadDigest(head))).toBe(c.digest);
    }
  });

  it("rejects a wrong key or a short signature", () => {
    const c = vectors.sth.heads[0] as (typeof vectors.sth.heads)[0];
    const head = c.head as TreeHead;
    const otherKey = new Uint8Array(32).fill(7);
    expect(verifyTreeHead(head, hexToBytes(c.signature), otherKey)).toBe(false);
    expect(verifyTreeHead(head, hexToBytes(c.signature).slice(1), publicKey)).toBe(false);
  });
});

describe("Receipt", () => {
  const receipt = vectors.receipt as unknown as ReceiptProof & { leaf_data: string };

  it("accepts the generated receipt", () => {
    expect(verifyReceiptProof(receipt, vectors.sth.public_key)).toEqual({
      inclusion: true,
      signature: true,
      ok: true,
    });
    expect(rootMatches(receipt.sth, receipt.sth.root_hex)).toBe(true);
  });

  it("rejects a changed leaf, a changed head or a wrong anchored root", () => {
    const otherLeaf = bytesToHex(leafHash(new TextEncoder().encode("forged")));
    expect(verifyReceiptProof({ ...receipt, leaf_hash: otherLeaf }, vectors.sth.public_key).ok).toBe(
      false,
    );
    const head = { ...receipt.sth, timestamp: receipt.sth.timestamp + 1 };
    const check = verifyReceiptProof({ ...receipt, sth: head }, vectors.sth.public_key);
    expect(check).toEqual({ inclusion: true, signature: false, ok: false });
    expect(rootMatches(receipt.sth, "00".repeat(32))).toBe(false);
    expect(rootMatches(receipt.sth, "zz")).toBe(false);
    expect(verifyReceiptProof({ ...receipt, leaf_hash: "zz" }, vectors.sth.public_key).ok).toBe(
      false,
    );
  });
});

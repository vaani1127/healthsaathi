import { ed25519 } from "@noble/curves/ed25519.js";
import { sha256 } from "@noble/hashes/sha2.js";

import { bytesToHex } from "./hex";
import { canonicalBytes } from "./jcs";

/** Signed tree head fields, exactly as signed (SPEC 6). */
export interface TreeHead {
  clinic_id: string;
  tree_size: number;
  root_hex: string;
  prev_root_hex: string | null;
  timestamp: number;
  key_id: string;
}

export function signingBytes(head: TreeHead): Uint8Array {
  return canonicalBytes({
    clinic_id: head.clinic_id,
    tree_size: head.tree_size,
    root_hex: head.root_hex,
    prev_root_hex: head.prev_root_hex,
    timestamp: head.timestamp,
    key_id: head.key_id,
  });
}

/** SHA-256 of the signed bytes: the value anchored on public witnesses. */
export function treeHeadDigest(head: TreeHead): Uint8Array {
  return sha256(signingBytes(head));
}

export function keyId(publicKey: Uint8Array): string {
  return bytesToHex(sha256(publicKey).slice(0, 8));
}

export function verifyTreeHead(
  head: TreeHead,
  signature: Uint8Array,
  publicKey: Uint8Array,
): boolean {
  if (publicKey.length !== 32 || signature.length !== 64) {
    return false;
  }
  if (head.key_id !== keyId(publicKey)) {
    return false;
  }
  try {
    return ed25519.verify(signature, signingBytes(head), publicKey);
  } catch {
    return false;
  }
}

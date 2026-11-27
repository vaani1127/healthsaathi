import { sha256 } from "@noble/hashes/sha2.js";

import { bytesToHex, hexToBytes } from "./hex";
import { canonicalBytes, type Json } from "./jcs";

/**
 * Commitment of one clinical record version (ADR 0011): SHA-256(salt || JCS(content)), as hex.
 * Mirrors app.ledger.records.commitment in the API.
 */
export function commitment(saltHex: string, content: { [key: string]: Json }): string {
  const salt = hexToBytes(saltHex);
  if (salt.length !== 32) {
    throw new Error("salt must be 32 bytes");
  }
  const body = canonicalBytes(content);
  const data = new Uint8Array(salt.length + body.length);
  data.set(salt, 0);
  data.set(body, salt.length);
  return bytesToHex(sha256(data));
}

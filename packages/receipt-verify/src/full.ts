import { sha256 } from "@noble/hashes/sha2.js";

import { bytesToHex, hexToBytes } from "./hex";
import { canonicalBytes, type Json } from "./jcs";
import { equalBytes, leafHash } from "./merkle";
import { verifyReceiptProof } from "./receipt";
import { type TreeHead, treeHeadDigest } from "./sth";

export interface AnchorRef {
  backend: string;
  status: string;
  tx_ref: string | null;
  block_ref: string | null;
  link: string | null;
}

/** A receipt as returned by GET /api/v1/access-events/{id}/receipt. */
export interface Receipt {
  version: number;
  clinic_id: string;
  clinic_id_bytes32: string;
  public_key: string;
  audit_seq: number;
  payload: { [key: string]: Json };
  payload_hash: string;
  leaf_hash: string;
  leaf_index: number;
  proof: string[];
  sth: TreeHead;
  signature: string;
  anchors: AnchorRef[];
}

export interface FullCheck {
  /** The payload hashes to payload_hash, and that to leaf_hash. */
  payload: boolean;
  /** The tree head is for the clinic the receipt names. */
  clinic: boolean;
  inclusion: boolean;
  signature: boolean;
  ok: boolean;
}

/** Everything that can be checked without the network. */
export function verifyReceipt(receipt: Receipt): FullCheck {
  let payload = false;
  try {
    const digest = sha256(canonicalBytes(receipt.payload));
    payload =
      equalBytes(digest, hexToBytes(receipt.payload_hash)) &&
      equalBytes(leafHash(digest), hexToBytes(receipt.leaf_hash));
  } catch {
    // Malformed hex or a payload that is not valid JSON.
  }
  const clinic = receipt.sth.clinic_id === receipt.clinic_id;
  const proof = verifyReceiptProof(receipt, receipt.public_key);
  return {
    payload,
    clinic,
    inclusion: proof.inclusion,
    signature: proof.signature,
    ok: payload && clinic && proof.ok,
  };
}

/** SHA-256 of the clinic public key, as stored in ClinicRegistry.signerKeyHash. */
export function publicKeyHash(publicKeyHex: string): string {
  return "0x" + bytesToHex(sha256(hexToBytes(publicKeyHex)));
}

export function sthDigestHex(head: TreeHead): string {
  return "0x" + bytesToHex(treeHeadDigest(head));
}

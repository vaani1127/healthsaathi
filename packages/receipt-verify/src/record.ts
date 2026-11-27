/**
 * Browser check of a clinical record's integrity response (ADR 0011), without trusting the
 * server's verdict. For each committed version: the leaf payload hashes to the stated leaf hash
 * and names this record and version, the inclusion proof reaches the signed tree head, and the
 * head is signed with the clinic key. For the newest version of each record, the commitment is
 * recomputed from the returned content and salt and compared with the leaf. The chain check is
 * separate (checkRecordAnchor) and reuses checkAnchor.
 */
import { sha256 } from "@noble/hashes/sha2.js";

import { type AnchorCheck, type ChainConfig, checkAnchor } from "./anchor";
import { commitment } from "./commitment";
import type { AnchorRef, Receipt } from "./full";
import { bytesToHex } from "./hex";
import { canonicalBytes, type Json } from "./jcs";
import { leafHash } from "./merkle";
import { verifyReceiptProof } from "./receipt";
import type { TreeHead } from "./sth";

type JsonObject = { [key: string]: Json };

/** One version as returned by GET /api/v1/records/{record_type}/{record_id}/integrity. */
export interface RecordVersionProof {
  record_id: string;
  version: number;
  status: string;
  reason: string | null;
  backfilled: boolean;
  committed_at: string | null;
  content_schema: string | null;
  content: JsonObject | null;
  salt: string | null;
  commitment: string | null;
  audit_seq: number | null;
  payload: JsonObject | null;
  payload_hash: string | null;
  leaf_hash: string | null;
  leaf_index: number | null;
  proof: string[] | null;
  sth: TreeHead | { [key: string]: Json } | null;
  signature: string | null;
  anchored: boolean;
  anchors: AnchorRef[];
}

export interface RecordCheckResponse {
  version: number;
  record_type: string;
  record_id: string;
  patient_id: string;
  verdict: string;
  reason: string | null;
  covered_since: string | null;
  backfilled: boolean;
  clinic_id: string;
  clinic_id_bytes32: string;
  public_key: string;
  versions: RecordVersionProof[];
}

export interface VersionCheck {
  record_id: string;
  version: number;
  /** The newest version of its record: its commitment is recomputed from the content. */
  current: boolean;
  /** The payload hashes to payload_hash and leaf_hash, and names this record, version and commitment. */
  leaf: boolean;
  /** null: no proof yet (the leaf is not in a signed checkpoint). */
  inclusion: boolean | null;
  signature: boolean | null;
  /** null for older versions, whose content is not returned. */
  commitment: boolean | null;
  backfilled: boolean;
  ok: boolean;
}

/**
 * verified: every check passed (the chain is checked separately).
 * pending: nothing failed, but at least one version is not in a signed checkpoint yet.
 * failed: a check failed, or a stored version has no leaf.
 * not_committed: the record has no committed version.
 */
export type BrowserVerdict = "verified" | "pending" | "failed" | "not_committed";

export interface RecordCheck {
  verdict: BrowserVerdict;
  versions: VersionCheck[];
  /** Every tree head is for the clinic the response names. */
  clinic: boolean;
  backfilled: boolean;
  coveredSince: string | null;
}

function leafMatches(response: RecordCheckResponse, v: RecordVersionProof): boolean {
  if (v.payload === null || v.payload_hash === null || v.leaf_hash === null) {
    return false;
  }
  try {
    const digest = sha256(canonicalBytes(v.payload));
    if (bytesToHex(digest) !== v.payload_hash.toLowerCase()) {
      return false;
    }
    if (bytesToHex(leafHash(digest)) !== v.leaf_hash.toLowerCase()) {
      return false;
    }
  } catch {
    return false;
  }
  const p = v.payload;
  return (
    p.type === "record_version" &&
    p.record_type === response.record_type &&
    p.record_id === v.record_id &&
    p.version === v.version &&
    typeof p.commitment === "string" &&
    p.commitment === v.commitment
  );
}

function newestPerRecord(versions: RecordVersionProof[]): Set<RecordVersionProof> {
  const newest = new Map<string, RecordVersionProof>();
  for (const v of versions) {
    const seen = newest.get(v.record_id);
    if (!seen || v.version > seen.version) {
      newest.set(v.record_id, v);
    }
  }
  return new Set(newest.values());
}

export function verifyRecordCheck(response: RecordCheckResponse): RecordCheck {
  const newest = newestPerRecord(response.versions);
  let clinic = true;
  const versions = response.versions.map((v): VersionCheck => {
    const leaf = leafMatches(response, v);
    let inclusion: boolean | null = null;
    let signature: boolean | null = null;
    if (v.proof !== null && v.sth !== null && v.signature !== null && v.leaf_hash !== null && v.leaf_index !== null) {
      const sth = v.sth as TreeHead;
      clinic = clinic && sth.clinic_id === response.clinic_id;
      const proof = verifyReceiptProof(
        { leaf_hash: v.leaf_hash, leaf_index: v.leaf_index, proof: v.proof, sth, signature: v.signature },
        response.public_key,
      );
      inclusion = proof.inclusion;
      signature = proof.signature;
    }
    const current = newest.has(v);
    let matches: boolean | null = null;
    if (current) {
      matches = false;
      if (v.content !== null && v.salt !== null && v.payload !== null) {
        try {
          matches = commitment(v.salt, v.content) === v.payload.commitment;
        } catch {
          matches = false;
        }
      }
    }
    const ok = leaf && inclusion !== false && signature !== false && matches !== false;
    return {
      record_id: v.record_id,
      version: v.version,
      current,
      leaf,
      inclusion,
      signature,
      commitment: matches,
      backfilled: v.backfilled || v.payload?.backfilled === true,
      ok,
    };
  });
  let verdict: BrowserVerdict;
  if (versions.length === 0) {
    verdict = "not_committed";
  } else if (!clinic || versions.some((v) => !v.ok)) {
    verdict = "failed";
  } else if (versions.some((v) => v.inclusion === null)) {
    verdict = "pending";
  } else {
    verdict = "verified";
  }
  return {
    verdict,
    versions,
    clinic,
    backfilled: versions.some((v) => v.backfilled),
    coveredSince: response.covered_since,
  };
}

/** One version's proof in the shape checkAnchor reads, or null when it has no proof yet. */
export function versionAsReceipt(response: RecordCheckResponse, v: RecordVersionProof): Receipt | null {
  if (v.sth === null || v.proof === null || v.signature === null || v.payload === null) {
    return null;
  }
  return {
    version: 1,
    clinic_id: response.clinic_id,
    clinic_id_bytes32: response.clinic_id_bytes32,
    public_key: response.public_key,
    audit_seq: v.audit_seq ?? 0,
    payload: v.payload,
    payload_hash: v.payload_hash ?? "",
    leaf_hash: v.leaf_hash ?? "",
    leaf_index: v.leaf_index ?? 0,
    proof: v.proof,
    sth: v.sth as TreeHead,
    signature: v.signature,
    anchors: v.anchors,
  };
}

/**
 * Checks every distinct tree head of the response against the AuditAnchor contract. mismatch if
 * any head disagrees with the chain, anchored if all are on chain, else not_anchored. Run
 * verifyRecordCheck first: this only says whether the chain agrees with the tree heads.
 */
export async function checkRecordAnchor(
  client: Parameters<typeof checkAnchor>[0],
  response: RecordCheckResponse,
  chain: ChainConfig,
): Promise<AnchorCheck | null> {
  const heads = new Map<number, Receipt>();
  for (const v of response.versions) {
    const receipt = versionAsReceipt(response, v);
    if (receipt !== null) {
      heads.set(receipt.sth.tree_size, receipt);
    }
  }
  if (heads.size === 0) {
    return null;
  }
  const results = await Promise.all([...heads.values()].map((r) => checkAnchor(client, r, chain)));
  const mismatch = results.find((r) => r.status === "mismatch");
  if (mismatch) {
    return mismatch;
  }
  const missing = results.find((r) => r.status === "not_anchored");
  const newest = results.at(-1) as AnchorCheck;
  const keyRegistered = results.some((r) => r.keyRegistered === false)
    ? false
    : (newest.keyRegistered ?? null);
  return { ...(missing ?? newest), keyRegistered };
}

/** True when a parsed JSON value has the shape of a record integrity response. */
export function isRecordCheckResponse(value: unknown): value is RecordCheckResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const r = value as Partial<RecordCheckResponse>;
  return (
    typeof r.record_type === "string" &&
    typeof r.record_id === "string" &&
    typeof r.clinic_id === "string" &&
    typeof r.public_key === "string" &&
    Array.isArray(r.versions)
  );
}

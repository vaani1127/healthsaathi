import {
  type AnchorCheck,
  checkRecordAnchor,
  isRecordCheckResponse,
  type RecordCheck,
  type RecordCheckResponse,
  verifyRecordCheck,
} from "@healthsaathi/receipt-verify";
import { createPublicClient, http } from "viem";

import type { Role } from "@/lib/api/client";
import { CHAIN } from "@/lib/config";

export type RecordType =
  | "note"
  | "prescription"
  | "vital"
  | "allergy"
  | "condition"
  | "lab_order"
  | "lab_result"
  | "document";

/**
 * Who may view each record type, mirroring apps/api/app/access/policy.yaml (view allowed without
 * break-glass). Only hides the button; the API decides and records every check.
 */
const VIEWERS: Record<RecordType, readonly Role[]> = {
  note: ["doctor", "patient"],
  condition: ["doctor", "patient"],
  prescription: ["doctor", "patient"],
  vital: ["doctor", "nurse", "patient"],
  allergy: ["doctor", "nurse", "patient"],
  lab_order: ["doctor", "lab_tech", "patient"],
  lab_result: ["doctor", "lab_tech", "patient"],
  document: ["doctor", "lab_tech", "patient"],
};

export function canCheck(role: Role | null | undefined, type: RecordType): boolean {
  return role != null && VIEWERS[type].includes(role);
}

/**
 * The browser's own verdict, from the response's proofs and the chain, never the server's.
 * verified: every check passed and the tree head is on chain (or no chain is configured here).
 * pending: nothing failed, but the newest version is not in a signed checkpoint or on chain yet.
 * failed: a check failed, or the chain has a different root.
 * not_committed: the record has no committed version.
 */
export type FinalVerdict = "verified" | "pending" | "failed" | "not_committed";

export interface RecordResult {
  response: RecordCheckResponse;
  offline: RecordCheck;
  /** null when no chain is configured here, or when no version has a proof yet. */
  anchor: AnchorCheck | null;
  anchorError: boolean;
  verdict: FinalVerdict;
}

function finalVerdict(offline: RecordCheck, anchor: AnchorCheck | null, anchorError: boolean): FinalVerdict {
  if (offline.verdict !== "verified") {
    return offline.verdict;
  }
  if (anchor?.status === "mismatch" || anchor?.keyRegistered === false) {
    return "failed";
  }
  if (anchor?.status === "not_anchored" || anchorError) {
    return "pending";
  }
  return "verified";
}

export async function checkRecord(response: RecordCheckResponse): Promise<RecordResult> {
  const offline = verifyRecordCheck(response);
  let anchor: AnchorCheck | null = null;
  let anchorError = false;
  if (CHAIN.rpcUrl && CHAIN.auditAnchor && offline.verdict !== "failed") {
    try {
      const client = createPublicClient({ transport: http(CHAIN.rpcUrl) });
      anchor = await checkRecordAnchor(client, response, {
        auditAnchor: CHAIN.auditAnchor,
        clinicRegistry: CHAIN.clinicRegistry ?? undefined,
      });
    } catch {
      anchorError = true;
    }
  }
  return { response, offline, anchor, anchorError, verdict: finalVerdict(offline, anchor, anchorError) };
}

export function parseRecordCheck(text: string): RecordCheckResponse | null {
  try {
    const value: unknown = JSON.parse(text);
    return isRecordCheckResponse(value) ? value : null;
  } catch {
    return null;
  }
}

export function downloadRecordCheck(response: RecordCheckResponse): void {
  const blob = new Blob([JSON.stringify(response, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `healthsaathi-record-${response.record_type}-${response.record_id}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

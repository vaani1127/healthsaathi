import {
  type AnchorCheck,
  checkAnchor,
  type FullCheck,
  type Receipt,
  verifyReceipt,
} from "@healthsaathi/receipt-verify";
import { createPublicClient, http } from "viem";

import { CHAIN } from "@/lib/config";

export interface ReceiptResult {
  offline: FullCheck;
  /** null when no chain is configured for this deployment. */
  anchor: AnchorCheck | null;
  anchorError: boolean;
}

export function parseReceipt(text: string): Receipt | null {
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null) {
      return null;
    }
    const r = value as Partial<Receipt>;
    const ok =
      typeof r.payload === "object" &&
      typeof r.payload_hash === "string" &&
      typeof r.leaf_hash === "string" &&
      typeof r.leaf_index === "number" &&
      Array.isArray(r.proof) &&
      typeof r.sth === "object" &&
      typeof r.signature === "string" &&
      typeof r.public_key === "string" &&
      typeof r.clinic_id === "string";
    return ok ? ({ anchors: [], ...r } as Receipt) : null;
  } catch {
    return null;
  }
}

export async function checkReceipt(receipt: Receipt): Promise<ReceiptResult> {
  const offline = verifyReceipt(receipt);
  if (!CHAIN.rpcUrl || !CHAIN.auditAnchor) {
    return { offline, anchor: null, anchorError: false };
  }
  try {
    const client = createPublicClient({ transport: http(CHAIN.rpcUrl) });
    const anchor = await checkAnchor(client, receipt, {
      auditAnchor: CHAIN.auditAnchor,
      clinicRegistry: CHAIN.clinicRegistry ?? undefined,
    });
    return { offline, anchor, anchorError: false };
  } catch {
    return { offline, anchor: null, anchorError: true };
  }
}

export function downloadReceipt(receipt: Receipt): void {
  const blob = new Blob([JSON.stringify(receipt, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `healthsaathi-receipt-${receipt.audit_seq}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

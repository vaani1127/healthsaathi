import type { Receipt } from "@healthsaathi/receipt-verify";
import { describe, expect, it, vi } from "vitest";

import vectors from "../../../packages/receipt-verify/test/vectors/ledger.json";
import { stats, timeChain, timeOffline } from "@/features/bench/bench";

const receipt = vectors.api_receipt as unknown as Receipt;

function ticking(step: number): () => number {
  let now = 0;
  return () => (now += step);
}

describe("receipt timing", () => {
  it("summarises samples by median and 95th percentile", () => {
    expect(stats([5, 1, 3])).toEqual({ runs: 3, median_ms: 3, p95_ms: 5 });
    expect(stats([1, 2, 3, 4])).toEqual({ runs: 4, median_ms: 2.5, p95_ms: 4 });
    const twenty = Array.from({ length: 20 }, (_, i) => i + 1);
    expect(stats(twenty).p95_ms).toBe(19);
  });

  it("times the offline checks and reports whether the receipt verified", () => {
    const result = timeOffline(receipt, 5, 1, ticking(2));
    expect(result).toMatchObject({ runs: 5, median_ms: 2, verified: true });
    const tampered = { ...receipt, payload: { ...receipt.payload, role: "nurse" } };
    expect(timeOffline(tampered, 2, 0, ticking(1)).verified).toBe(false);
  });

  it("makes the same chain reads as a real check, for the live clinic and transaction", async () => {
    const readContract = vi.fn().mockResolvedValue("0x" + "00".repeat(32));
    const getTransactionReceipt = vi.fn().mockResolvedValue({ logs: [] });
    const client = { readContract, getTransactionReceipt } as unknown as Parameters<typeof timeChain>[0];
    const probe = { chain: "amoy", clinic_id_bytes32: "0x" + "ab".repeat(32), tx_ref: "0x" + "cd".repeat(32) };
    const chain = { auditAnchor: ("0x" + "11".repeat(20)) as `0x${string}`, clinicRegistry: ("0x" + "22".repeat(20)) as `0x${string}` };

    const result = await timeChain(client, receipt, probe, chain, 3, ticking(10));
    expect(result).toMatchObject({ runs: 3, errors: 0, median_ms: 10 });
    expect(readContract).toHaveBeenCalledWith(expect.objectContaining({ args: [probe.clinic_id_bytes32] }));
    expect(getTransactionReceipt).toHaveBeenCalledWith({ hash: probe.tx_ref });
  });

  it("counts failed chain reads instead of timing them", async () => {
    const client = {
      readContract: vi.fn().mockRejectedValue(new Error("offline")),
      getTransactionReceipt: vi.fn(),
    } as unknown as Parameters<typeof timeChain>[0];
    const probe = { chain: "amoy", clinic_id_bytes32: "0x" + "ab".repeat(32), tx_ref: "0x" + "cd".repeat(32) };
    const result = await timeChain(client, receipt, probe, { auditAnchor: ("0x" + "11".repeat(20)) as `0x${string}`, clinicRegistry: ("0x" + "22".repeat(20)) as `0x${string}` }, 2);
    expect(result.errors).toBe(2);
    expect(result.runs).toBe(0);
  });
});

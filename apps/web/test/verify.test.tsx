import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import vectors from "../../../packages/receipt-verify/test/vectors/ledger.json";
import { checkReceipt, parseReceipt } from "@/features/verify/check";
import { ReceiptResultView } from "@/features/verify/ReceiptResultView";

const text = JSON.stringify(vectors.api_receipt);

function receipt() {
  const parsed = parseReceipt(text);
  if (parsed === null) {
    throw new Error("vector did not parse");
  }
  return parsed;
}

describe("receipt check", () => {
  it("parses a receipt and rejects anything else", () => {
    expect(parseReceipt(text)?.audit_seq).toBe(vectors.api_receipt.audit_seq);
    expect(parseReceipt("not json")).toBeNull();
    expect(parseReceipt('{"hello": 1}')).toBeNull();
    expect(parseReceipt("null")).toBeNull();
  });

  it("verifies offline and skips the chain when none is configured", async () => {
    const result = await checkReceipt(receipt());
    expect(result.offline.ok).toBe(true);
    expect(result.anchor).toBeNull();

    render(<ReceiptResultView result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("Verified");
    expect(screen.getByText(/No public chain/)).toBeInTheDocument();
  });

  it("shows a tampered receipt as not verified", async () => {
    const r = receipt();
    const result = await checkReceipt({ ...r, payload: { ...r.payload, role: "nurse" } });
    render(<ReceiptResultView result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("Not verified");
  });
});

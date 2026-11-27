import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import vectors from "../../../packages/receipt-verify/test/record-vectors.json";
import { canCheck, checkRecord, parseRecordCheck, type RecordType } from "@/features/verify/recordCheck";
import { RecordResultView } from "@/features/verify/RecordResultView";
import en from "@/locales/en.json";
import hi from "@/locales/hi.json";

const text = JSON.stringify(vectors.record_check);

function response() {
  const parsed = parseRecordCheck(text);
  if (parsed === null) {
    throw new Error("vector did not parse");
  }
  return parsed;
}

describe("record check", () => {
  it("parses a record check file and rejects anything else", () => {
    expect(parseRecordCheck(text)?.record_type).toBe("lab_result");
    expect(parseRecordCheck(JSON.stringify({ payload: {} }))).toBeNull();
    expect(parseRecordCheck("not json")).toBeNull();
  });

  it("gives the browser's own verdict, not the server's", async () => {
    const result = await checkRecord({ ...response(), verdict: "mismatch" });
    expect(result.verdict).toBe("verified");
    render(<RecordResultView result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("Verified");
    expect(screen.getAllByTestId("record-version")).toHaveLength(2);
  });

  it("fails a changed record even when the server says match", async () => {
    const r = response();
    const newest = r.versions[1];
    if (!newest?.content) {
      throw new Error("vector has no content");
    }
    newest.content = { ...newest.content, values: {} };
    const result = await checkRecord(r);
    expect(result.verdict).toBe("failed");
    render(<RecordResultView result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("Not verified");
  });

  it("says clearly when versions were added later", async () => {
    const r = response();
    for (const v of r.versions) {
      v.backfilled = true;
    }
    render(<RecordResultView result={await checkRecord(r)} />);
    expect(screen.getByTestId("record-backfilled")).toHaveTextContent("It says nothing about changes before");
  });

  it("explains pending in plain words", async () => {
    const r = response();
    const newest = r.versions[1];
    if (!newest) {
      throw new Error("no version");
    }
    newest.proof = null;
    newest.sth = null;
    newest.signature = null;
    const result = await checkRecord(r);
    expect(result.verdict).toBe("pending");
    render(<RecordResultView result={result} />);
    expect(screen.getByText(/Check again in about 15 minutes/)).toBeInTheDocument();
  });
});

describe("who sees the button", () => {
  // Mirrors the view rules in apps/api/app/access/policy.yaml; the API still decides.
  // vitest runs from apps/web.
  const policy = readFileSync(resolve(process.cwd(), "../api/app/access/policy.yaml"), "utf-8");
  const resource: Record<RecordType, string> = {
    note: "notes",
    condition: "notes",
    prescription: "prescriptions",
    vital: "vitals",
    allergy: "allergies",
    lab_order: "lab",
    lab_result: "lab",
    document: "documents",
  };

  function policyAllowsView(role: string, res: string): boolean {
    const block = policy.split(/\n {2}(?=\w+:\n)/).find((b) => b.trimStart().startsWith(`${role}:`)) ?? "";
    const line = block.split("\n").find((l) => l.trim().startsWith(`${res}:`)) ?? "";
    return /view: (none|explanation_or_review)/.test(line);
  }

  it.each(["reception", "nurse", "doctor", "lab_tech", "clinic_admin", "patient"] as const)(
    "matches policy.yaml for %s",
    (role) => {
      for (const type of Object.keys(resource) as RecordType[]) {
        expect(canCheck(role, type)).toBe(policyAllowsView(role, resource[type]));
      }
    },
  );
});

describe("wording", () => {
  it("never claims more than tamper-evident", () => {
    const all = (JSON.stringify(en) + JSON.stringify(hi)).toLowerCase();
    for (const word of ["tamper-proof", "immutable", "blockchain-secured"]) {
      expect(all).not.toContain(word);
    }
    expect(JSON.stringify(en.record)).toContain("tamper-evident");
  });
});

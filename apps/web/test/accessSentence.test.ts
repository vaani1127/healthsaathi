import { afterEach, describe, expect, it } from "vitest";

import { accessSentence } from "@/features/patient/explain";
import i18n from "@/i18n";
import type { Schemas } from "@/lib/api/client";

const base: Schemas["AccessLogEntry"] = {
  id: "e1",
  at: "2026-10-08T06:35:00Z",
  user_name: "Doctor Demo",
  role: "doctor",
  resource: "notes",
  action: "view",
  decision: "allow",
  break_glass: false,
  because: { template: "T_APPT", kind: "appointment", token_no: 23, slot_start: "2026-10-08T06:35:00Z" },
  query_status: null,
};

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("accessSentence", () => {
  it("names who, what, why with the token and time", () => {
    const text = accessSentence(base);
    expect(text).toMatch(/^Doctor Demo \(Doctor\) opened your notes because of appointment token 23 at /);
  });

  it("says when nothing explains the access", () => {
    expect(accessSentence({ ...base, because: { template: null } })).toContain(
      "No appointment or other reason was found",
    );
  });

  it("covers refusals, reasons and other templates", () => {
    expect(accessSentence({ ...base, decision: "deny" })).toContain("the access was refused");
    expect(
      accessSentence({ ...base, because: { template: "T_REASON", reason_code: "covering_doctor" } }),
    ).toContain("Covering for another doctor");
    expect(accessSentence({ ...base, role: "lab_tech", resource: "lab", because: { template: "T_LAB", tests: ["CBC"] } })).toContain(
      "because of your lab test: CBC",
    );
    expect(accessSentence({ ...base, because: { template: "T_BREAKGLASS" } })).toContain("emergency access");
  });

  it("is written in Hindi when Hindi is chosen", async () => {
    await i18n.changeLanguage("hi");
    const text = accessSentence(base);
    expect(text).toContain("नोट");
    expect(text).toContain("टोकन 23");
  });
});

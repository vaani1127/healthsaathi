import { describe, expect, it } from "vitest";

import { clinicDay } from "@/lib/clinicDay";

describe("clinic day", () => {
  it("is the date in the clinic's timezone, not the device's", () => {
    // 00:30 on 2 November in India, still 1 November in UTC.
    const at = new Date("2026-11-01T19:00:00Z");
    expect(clinicDay("Asia/Kolkata", at)).toBe("2026-11-02");
    expect(clinicDay("UTC", at)).toBe("2026-11-01");
    // 05:00 in India on 2 November is 23:30 UTC on 1 November.
    expect(clinicDay("Asia/Kolkata", new Date("2026-11-01T23:30:00Z"))).toBe("2026-11-02");
  });

  it("falls back to the device's date without a usable timezone", () => {
    const at = new Date(2026, 10, 2, 12, 0);
    expect(clinicDay(undefined, at)).toBe("2026-11-02");
    expect(clinicDay("Not/AZone", at)).toBe("2026-11-02");
  });
});

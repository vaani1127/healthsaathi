import { describe, expect, it } from "vitest";

import en from "@/locales/en.json";
import hi from "@/locales/hi.json";

function keys(obj: object, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    typeof v === "object" && v !== null ? keys(v as object, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}

describe("locales", () => {
  it("hi has exactly the same keys as en", () => {
    expect(keys(hi).sort()).toEqual(keys(en).sort());
  });

  it("has no empty strings", () => {
    for (const dict of [en, hi]) {
      const flat = JSON.stringify(dict);
      expect(flat).not.toContain('""');
    }
  });
});

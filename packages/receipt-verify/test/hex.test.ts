import { describe, expect, it } from "vitest";

import { bytesToHex, hexToBytes } from "../src";

describe("hex helpers", () => {
  it("round trips bytes", () => {
    const bytes = new Uint8Array([0, 1, 15, 16, 255]);
    expect(bytesToHex(bytes)).toBe("00010f10ff");
    expect(hexToBytes("00010f10ff")).toEqual(bytes);
  });

  it("accepts a 0x prefix", () => {
    expect(hexToBytes("0xff00")).toEqual(new Uint8Array([255, 0]));
  });

  it("rejects bad input", () => {
    expect(() => hexToBytes("abc")).toThrow();
    expect(() => hexToBytes("zz")).toThrow();
  });
});

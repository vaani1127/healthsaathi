const HEX = /^(0x)?([0-9a-fA-F]{2})*$/;

export function hexToBytes(hex: string): Uint8Array {
  if (!HEX.test(hex)) {
    throw new Error("invalid hex string");
  }
  const clean = hex.startsWith("0x") ? hex.slice(2) : hex;
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = Number.parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

export function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) {
    out += b.toString(16).padStart(2, "0");
  }
  return out;
}

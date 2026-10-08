/**
 * RFC 8785 JSON Canonicalization Scheme. JavaScript already formats numbers and escapes strings
 * the way the RFC asks, so only key order and whitespace need handling here.
 */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export function canonicalize(value: Json): string {
  if (value === null || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("NaN and Infinity are not valid JSON");
    }
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    if (!value.isWellFormed()) {
      throw new Error("strings must be valid Unicode");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalize).join(",")}]`;
  }
  // Default string sort compares UTF-16 code units, which is the order RFC 8785 requires.
  const keys = Object.keys(value).sort();
  return `{${keys
    .map((k) => `${JSON.stringify(k)}:${canonicalize(value[k] as Json)}`)
    .join(",")}}`;
}

export function canonicalBytes(value: Json): Uint8Array {
  return new TextEncoder().encode(canonicalize(value));
}

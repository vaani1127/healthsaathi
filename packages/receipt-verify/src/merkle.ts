/**
 * RFC 6962 Merkle proof verification (algorithms from RFC 9162 sections 2.1.3.2 and 2.1.4.2).
 * Mirrors packages/e2d-core/src/e2d_core/ledger/merkle.py.
 */
import { sha256 } from "@noble/hashes/sha2.js";

export const HASH_SIZE = 32;

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

export function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= (a[i] as number) ^ (b[i] as number);
  }
  return diff === 0;
}

export function leafHash(data: Uint8Array): Uint8Array {
  return sha256(concat(new Uint8Array([0]), data));
}

export function nodeHash(left: Uint8Array, right: Uint8Array): Uint8Array {
  return sha256(concat(new Uint8Array([1]), left, right));
}

/** Tree sizes stay well below 2^53, so plain number arithmetic is exact. */
const isOdd = (n: number): boolean => n % 2 === 1;
const half = (n: number): number => Math.floor(n / 2);

export function verifyInclusion(
  leaf: Uint8Array,
  index: number,
  size: number,
  proof: Uint8Array[],
  root: Uint8Array,
): boolean {
  if (!Number.isSafeInteger(index) || !Number.isSafeInteger(size) || index < 0 || index >= size) {
    return false;
  }
  let fn = index;
  let sn = size - 1;
  let r = leaf;
  for (const p of proof) {
    if (p.length !== HASH_SIZE || sn === 0) {
      return false;
    }
    if (isOdd(fn) || fn === sn) {
      r = nodeHash(p, r);
      if (!isOdd(fn)) {
        while (!isOdd(fn) && fn !== 0) {
          fn = half(fn);
          sn = half(sn);
        }
      }
    } else {
      r = nodeHash(r, p);
    }
    fn = half(fn);
    sn = half(sn);
  }
  return sn === 0 && equalBytes(r, root);
}

export function verifyConsistency(
  oldSize: number,
  newSize: number,
  oldRoot: Uint8Array,
  newRoot: Uint8Array,
  proof: Uint8Array[],
): boolean {
  if (oldSize < 0 || newSize < oldSize) {
    return false;
  }
  if (oldSize === newSize) {
    return proof.length === 0 && equalBytes(oldRoot, newRoot);
  }
  if (oldSize === 0) {
    return proof.length === 0;
  }
  if (proof.length === 0 || proof.some((p) => p.length !== HASH_SIZE)) {
    return false;
  }
  const path = [...proof];
  if ((oldSize & (oldSize - 1)) === 0) {
    path.unshift(oldRoot);
  }
  let fn = oldSize - 1;
  let sn = newSize - 1;
  while (isOdd(fn)) {
    fn = half(fn);
    sn = half(sn);
  }
  let fr = path[0] as Uint8Array;
  let sr = fr;
  for (const c of path.slice(1)) {
    if (sn === 0) {
      return false;
    }
    if (isOdd(fn) || fn === sn) {
      fr = nodeHash(c, fr);
      sr = nodeHash(c, sr);
      if (!isOdd(fn)) {
        while (!isOdd(fn) && fn !== 0) {
          fn = half(fn);
          sn = half(sn);
        }
      }
    } else {
      sr = nodeHash(sr, c);
    }
    fn = half(fn);
    sn = half(sn);
  }
  return sn === 0 && equalBytes(fr, oldRoot) && equalBytes(sr, newRoot);
}

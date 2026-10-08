import { hexToBytes } from "./hex";
import { equalBytes, verifyInclusion } from "./merkle";
import { type TreeHead, verifyTreeHead } from "./sth";

/** The parts of a patient receipt that can be checked offline. */
export interface ReceiptProof {
  leaf_hash: string;
  leaf_index: number;
  proof: string[];
  sth: TreeHead;
  signature: string;
}

export interface ReceiptCheck {
  inclusion: boolean;
  signature: boolean;
  ok: boolean;
}

/**
 * Checks that the leaf is in the tree described by the signed head, and that the head is signed
 * by the clinic key. Comparing the root with the on-chain value is done separately (anchor check).
 */
export function verifyReceiptProof(receipt: ReceiptProof, clinicPublicKey: string): ReceiptCheck {
  let inclusion = false;
  let signature = false;
  try {
    inclusion = verifyInclusion(
      hexToBytes(receipt.leaf_hash),
      receipt.leaf_index,
      receipt.sth.tree_size,
      receipt.proof.map(hexToBytes),
      hexToBytes(receipt.sth.root_hex),
    );
    signature = verifyTreeHead(
      receipt.sth,
      hexToBytes(receipt.signature),
      hexToBytes(clinicPublicKey),
    );
  } catch {
    // Malformed hex means the receipt is not valid.
  }
  return { inclusion, signature, ok: inclusion && signature };
}

export function rootMatches(head: TreeHead, anchoredRootHex: string): boolean {
  try {
    return equalBytes(hexToBytes(head.root_hex), hexToBytes(anchoredRootHex));
  } catch {
    return false;
  }
}

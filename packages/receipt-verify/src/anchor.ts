import { decodeEventLog, type Hex, isAddressEqual, parseAbi, type PublicClient } from "viem";

import { type Receipt, publicKeyHash, sthDigestHex } from "./full";

export const auditAnchorAbi = parseAbi([
  "function latest(bytes32 clinicId) view returns (uint64 treeSize, bytes32 root)",
  "event Anchored(bytes32 indexed clinicId, uint64 treeSize, bytes32 root, bytes32 sthDigest, uint256 timestamp)",
]);

export const clinicRegistryAbi = parseAbi([
  "function signerKeyHash(bytes32 clinicId) view returns (bytes32)",
]);

export interface ChainConfig {
  auditAnchor: Hex;
  clinicRegistry?: Hex;
}

export type AnchorStatus =
  /** An Anchored event for exactly this tree head is on chain. */
  | "anchored"
  /** The chain has nothing for this tree head yet (a newer or older head may be there). */
  | "not_anchored"
  /** The chain has a different root for the same tree size: the log was changed. */
  | "mismatch";

export interface AnchorCheck {
  status: AnchorStatus;
  /** The registry holds the hash of the receipt's public key; null if no registry was given. */
  keyRegistered: boolean | null;
  txHash: string | null;
  blockNumber: bigint | null;
}

type ChainReader = Pick<PublicClient, "readContract" | "getTransactionReceipt">;

const lower = (value: string) => value.toLowerCase();

/**
 * Compares the receipt's signed tree head with the AuditAnchor contract. When the receipt names
 * the anchoring transaction, its Anchored event is checked; otherwise the latest anchored head is.
 * Run verifyReceipt first: this only says whether the chain agrees with the tree head.
 */
export async function checkAnchor(
  client: ChainReader,
  receipt: Receipt,
  chain: ChainConfig,
): Promise<AnchorCheck> {
  const clinicId = receipt.clinic_id_bytes32 as Hex;
  const root = lower("0x" + receipt.sth.root_hex);
  const digest = lower(sthDigestHex(receipt.sth));

  let keyRegistered: boolean | null = null;
  if (chain.clinicRegistry) {
    const onChain = await client.readContract({
      address: chain.clinicRegistry,
      abi: clinicRegistryAbi,
      functionName: "signerKeyHash",
      args: [clinicId],
    });
    keyRegistered = lower(onChain) === lower(publicKeyHash(receipt.public_key));
  }

  const txHash = receipt.anchors.find((a) => a.backend !== "github" && a.backend !== "ots")?.tx_ref;
  if (txHash) {
    const mined = await client.getTransactionReceipt({ hash: txHash as Hex });
    for (const log of mined.logs) {
      if (!isAddressEqual(log.address, chain.auditAnchor)) {
        continue;
      }
      let event;
      try {
        event = decodeEventLog({ abi: auditAnchorAbi, data: log.data, topics: log.topics });
      } catch {
        continue;
      }
      const args = event.args;
      if (lower(args.clinicId) !== lower(clinicId) || Number(args.treeSize) !== receipt.sth.tree_size) {
        continue;
      }
      const same = lower(args.root) === root && lower(args.sthDigest) === digest;
      return {
        status: same ? "anchored" : "mismatch",
        keyRegistered,
        txHash,
        blockNumber: mined.blockNumber,
      };
    }
  }

  const [size, latestRoot] = await client.readContract({
    address: chain.auditAnchor,
    abi: auditAnchorAbi,
    functionName: "latest",
    args: [clinicId],
  });
  let status: AnchorStatus = "not_anchored";
  if (Number(size) === receipt.sth.tree_size) {
    status = lower(latestRoot) === root ? "anchored" : "mismatch";
  }
  return { status, keyRegistered, txHash: null, blockNumber: null };
}

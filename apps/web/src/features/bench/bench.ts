import {
  type ChainConfig,
  checkAnchor,
  type Receipt,
  verifyReceipt,
} from "@healthsaathi/receipt-verify";

/** apps/web/public/bench/receipts.json, written by `python -m e2d_experiments.anchoring
 * bench-receipts`. */
export interface BenchFile {
  receipts: { tree_size: number; receipt: Receipt }[];
  chain_probe: { chain: string; clinic_id_bytes32: string; tx_ref: string } | null;
}

export interface StepStats {
  runs: number;
  median_ms: number;
  p95_ms: number;
}

export interface BenchResult {
  label: string;
  user_agent: string;
  cpus: number | null;
  memory_gb: number | null;
  started_at: string;
  offline: ({ tree_size: number; proof_hashes: number; verified: boolean } & StepStats)[];
  chain: (StepStats & { errors: number }) | null;
}

type Clock = () => number;

export function stats(samples: number[]): StepStats {
  const sorted = [...samples].sort((a, b) => a - b);
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.ceil(q * sorted.length) - 1)] ?? 0;
  const mid = Math.floor(sorted.length / 2);
  const median =
    sorted.length % 2 === 1 ? (sorted[mid] ?? 0) : ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2;
  return { runs: sorted.length, median_ms: round(median), p95_ms: round(at(0.95)) };
}

function round(ms: number): number {
  return Math.round(ms * 1000) / 1000;
}

/** Times the checks a patient's browser runs without the network (payload, inclusion proof, tree
 * head signature), the same verifyReceipt the "Check receipt" button uses. */
export function timeOffline(receipt: Receipt, runs: number, warmup = 3, clock: Clock = () => performance.now()) {
  let verified = true;
  for (let i = 0; i < warmup; i++) {
    verified = verifyReceipt(receipt).ok && verified;
  }
  const samples: number[] = [];
  for (let i = 0; i < runs; i++) {
    const start = clock();
    verified = verifyReceipt(receipt).ok && verified;
    samples.push(clock() - start);
  }
  return { ...stats(samples), verified };
}

type ChainReader = Parameters<typeof checkAnchor>[0];

/** Times the chain reads of a real check (registered key, anchoring transaction) for a live clinic
 * and anchor. The bench receipt's own root is not on chain, so only the time is used. */
export async function timeChain(
  client: ChainReader,
  receipt: Receipt,
  probe: NonNullable<BenchFile["chain_probe"]>,
  chain: ChainConfig,
  runs: number,
  clock: Clock = () => performance.now(),
) {
  const probing: Receipt = {
    ...receipt,
    clinic_id_bytes32: probe.clinic_id_bytes32,
    anchors: [{ backend: probe.chain, status: "confirmed", tx_ref: probe.tx_ref, block_ref: null, link: null }],
  };
  const samples: number[] = [];
  let errors = 0;
  for (let i = 0; i < runs; i++) {
    const start = clock();
    try {
      await checkAnchor(client, probing, chain);
      samples.push(clock() - start);
    } catch {
      errors += 1;
    }
  }
  return { ...stats(samples), errors };
}

export function deviceInfo(): Pick<BenchResult, "user_agent" | "cpus" | "memory_gb"> {
  const nav = navigator as Navigator & { deviceMemory?: number };
  return {
    user_agent: nav.userAgent,
    cpus: nav.hardwareConcurrency || null,
    memory_gb: nav.deviceMemory ?? null,
  };
}

export function downloadResult(result: BenchResult): void {
  const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const name = result.label.replace(/[^a-z0-9]+/gi, "-").toLowerCase() || "device";
  a.download = `receipt-bench-${name}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

/** Where receipts are checked on chain. Empty when the deployment has no chain configured. */
export const CHAIN = {
  rpcUrl: import.meta.env.VITE_CHAIN_RPC_URL || null,
  auditAnchor: (import.meta.env.VITE_AUDIT_ANCHOR_ADDRESS || null) as `0x${string}` | null,
  clinicRegistry: (import.meta.env.VITE_CLINIC_REGISTRY_ADDRESS || null) as `0x${string}` | null,
  explorerUrl: import.meta.env.VITE_EXPLORER_URL || null,
};

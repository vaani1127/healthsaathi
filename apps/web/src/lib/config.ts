/** Runtime settings from /config.js win over build-time VITE_* values, so one build can be
 * pointed at a different chain (for example the local demo's anvil) without rebuilding. */
const runtime: RuntimeConfig = (typeof window !== "undefined" && window.__HS_CONFIG__) || {};

export const API_URL: string = runtime.apiUrl ?? import.meta.env.VITE_API_URL ?? "http://localhost:8000";

/** Where receipts are checked on chain. Empty when the deployment has no chain configured. */
export const CHAIN = {
  rpcUrl: runtime.chainRpcUrl || import.meta.env.VITE_CHAIN_RPC_URL || null,
  auditAnchor: (runtime.auditAnchorAddress || import.meta.env.VITE_AUDIT_ANCHOR_ADDRESS || null) as `0x${string}` | null,
  clinicRegistry: (runtime.clinicRegistryAddress || import.meta.env.VITE_CLINIC_REGISTRY_ADDRESS || null) as
    | `0x${string}`
    | null,
  explorerUrl: runtime.explorerUrl || import.meta.env.VITE_EXPLORER_URL || null,
};

/** The dev mailbox page is offered only in local development and the local demo. */
export const DEV_MAILBOX: boolean = Boolean(runtime.devMailbox) || import.meta.env.DEV === true;

/** WebSocket URL of the API; works when the API is on the same origin (API_URL empty). */
export function wsUrl(path: string): string {
  const base = API_URL || window.location.origin;
  return `${base.replace(/^http/, "ws")}${path}`;
}

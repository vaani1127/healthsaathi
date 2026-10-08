interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_CHAIN_RPC_URL?: string;
  readonly VITE_AUDIT_ANCHOR_ADDRESS?: string;
  readonly VITE_CLINIC_REGISTRY_ADDRESS?: string;
  readonly VITE_EXPLORER_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

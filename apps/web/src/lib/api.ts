import { API_URL } from "./config";

export interface HealthResponse {
  status: "ok";
  version: string;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const resp = await fetch(`${API_URL}/api/v1/health`, { signal: signal ?? null });
  if (!resp.ok) {
    throw new Error(`health check failed with status ${resp.status}`);
  }
  return (await resp.json()) as HealthResponse;
}

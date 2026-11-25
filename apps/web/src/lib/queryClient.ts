import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";

/** Queries retry only on network errors and 5xx, at most twice. A 4xx answer, including 429
 * (rate limited), is final: retrying would only use up more of the limit. Mutations never retry
 * (the TanStack Query default). */
export function shouldRetry(count: number, error: unknown): boolean {
  return !(error instanceof ApiError && error.status < 500 && error.status > 0) && count < 2;
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        refetchOnWindowFocus: false,
      },
    },
  });
}

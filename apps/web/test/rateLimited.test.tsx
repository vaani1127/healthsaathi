import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoadError } from "@/components/ui/state";
import i18n from "@/i18n";
import { api, call } from "@/lib/api/client";
import { makeQueryClient } from "@/lib/queryClient";

function Health({ fetch }: { fetch: (request: Request) => Promise<Response> }) {
  // openapi-fetch keeps the fetch it had when the client was made, so the stub goes per request.
  const health = useQuery({ queryKey: ["health"], queryFn: () => call(() => api.GET("/api/v1/health", { fetch })) });
  if (health.isError) {
    return <LoadError error={health.error} />;
  }
  return <p>{health.isPending ? "loading" : "ok"}</p>;
}

afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("a 429 answer", () => {
  it("shows the rate limit message and is not retried", async () => {
    const fetch = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ type: "https://healthsaathi.dev/problems/rate-limited", title: "Too Many Requests", status: 429, code: "rate-limited" }),
          { status: 429, headers: { "content-type": "application/problem+json", "retry-after": "42" } },
        ),
      ),
    );
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <Health fetch={fetch} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(i18n.t("errors.rate-limited"))).toBeInTheDocument();
    // Give a retry the chance to happen: there is none.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.getByText(i18n.t("errors.rate-limited"))).toBeInTheDocument();
  });

  it("is shown in Hindi too", async () => {
    await i18n.changeLanguage("hi");
    expect(i18n.t("errors.rate-limited")).not.toBe("errors.rate-limited");
    expect(i18n.t("errors.rate-limited")).not.toBe(i18n.getFixedT("en")("errors.rate-limited"));
  });
});

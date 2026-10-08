import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HomePage } from "@/features/home/HomePage";
import i18n from "@/i18n";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HomePage />
    </QueryClientProvider>,
  );
}

function stubHealthy() {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(Response.json({ status: "ok", version: "0.1.0" }))),
  );
}

afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("HomePage", () => {
  it("shows API health OK when the server answers", async () => {
    stubHealthy();
    renderPage();
    expect(await screen.findByText("API health OK")).toBeInTheDocument();
    expect(screen.getByText("API version 0.1.0")).toBeInTheDocument();
  });

  it("shows an error when the server is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("network"))),
    );
    renderPage();
    // The page retries once before showing the error.
    expect(
      await screen.findByText("Cannot reach the server", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
  });

  it("switches to Hindi", async () => {
    stubHealthy();
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "हिन्दी" }));
    expect(await screen.findByText("एपीआई ठीक है")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("hi");
  });
});

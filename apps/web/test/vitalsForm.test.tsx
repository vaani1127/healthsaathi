import "fake-indexeddb/auto";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VitalsForm } from "@/features/nurse/VitalsForm";
import i18n from "@/i18n";
import { api } from "@/lib/api/client";

function renderForm() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <VitalsForm patientId="00000000-0000-0000-0000-000000000001" />
    </QueryClientProvider>,
  );
}

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

describe("VitalsForm", () => {
  it("cannot be saved empty and says why", async () => {
    const post = vi.spyOn(api, "POST");
    renderForm();
    const save = screen.getByRole("button", { name: "Save vitals" });
    expect(save).toBeDisabled();
    expect(save).toHaveAccessibleDescription("Fill in at least one value to save.");
    await userEvent.type(screen.getByLabelText("Pulse"), "76");
    expect(save).toBeEnabled();
    expect(screen.queryByText("Fill in at least one value to save.")).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("marks the field the server names", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({
      response: new Response(null, { status: 422 }),
      error: { code: "validation-error", errors: [{ loc: ["body", "bp_sys"], msg: "too high" }] },
    } as never);
    renderForm();
    await userEvent.type(screen.getByLabelText("BP systolic"), "300");
    await userEvent.type(screen.getByLabelText("BP diastolic"), "80");
    await userEvent.click(screen.getByRole("button", { name: "Save vitals" }));
    expect(await screen.findByText("Check this value: it is outside the range the clinic accepts.")).toBeInTheDocument();
    expect(screen.getAllByText("Check this value: it is outside the range the clinic accepts.")).toHaveLength(1);
  });

  it("gives the hint in Hindi", async () => {
    await i18n.changeLanguage("hi");
    renderForm();
    expect(screen.getByText("सहेजने के लिए कम से कम एक मान भरें।")).toBeInTheDocument();
  });
});

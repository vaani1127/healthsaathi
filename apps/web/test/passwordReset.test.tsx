import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FunctionComponent } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ForgotPasswordPage } from "@/features/auth/ForgotPasswordPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { readResetLink } from "@/features/auth/resetLink";
import { ResetPasswordPage } from "@/features/auth/ResetPasswordPage";
import i18n from "@/i18n";
import { api } from "@/lib/api/client";

const TOKEN = "tok_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
const SAME_ANSWER = "If this email has an account, we sent a link. It works for 30 minutes. Check your inbox.";

function renderPage(Page: FunctionComponent) {
  const rootRoute = createRootRoute({ component: Page });
  const stub = (path: string) => createRoute({ getParentRoute: () => rootRoute, path, component: () => null });
  const router = createRouter({
    routeTree: rootRoute.addChildren([stub("/login"), stub("/forgot-password"), stub("/patient/login")]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function answer(status: number, error?: unknown) {
  return { response: new Response(null, { status }), data: status < 300 ? {} : undefined, error } as never;
}

beforeAll(() => {
  // jsdom has no scrolling; the router calls it on every navigation.
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
});

afterEach(async () => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
  await i18n.changeLanguage("en");
});

describe("sign-in page", () => {
  it("links to the forgot password page", async () => {
    renderPage(LoginPage);
    expect(await screen.findByRole("link", { name: "Forgot password?" })).toHaveAttribute("href", "/forgot-password");
  });
});

describe("ForgotPasswordPage", () => {
  it("shows the same message after sending, for any email", async () => {
    const post = vi.spyOn(api, "POST").mockResolvedValue(answer(202));
    for (const email of ["nobody@example.test", "nurse1.a@demo.healthsaathi.test"]) {
      const { unmount } = renderPage(ForgotPasswordPage);
      await userEvent.type(await screen.findByLabelText("Email"), email);
      await userEvent.click(screen.getByRole("button", { name: "Send link" }));
      expect(await screen.findByText(SAME_ANSWER)).toBeInTheDocument();
      unmount();
    }
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith("/api/v1/auth/password-reset/request", {
      body: { email: "nurse1.a@demo.healthsaathi.test" },
    });
  });

  it("is in Hindi when the language is Hindi", async () => {
    await i18n.changeLanguage("hi");
    renderPage(ForgotPasswordPage);
    expect(await screen.findByRole("button", { name: "लिंक भेजें" })).toBeInTheDocument();
  });
});

describe("ResetPasswordPage", () => {
  function openLink() {
    window.history.replaceState(null, "", `/reset-password#email=nurse%40clinic.test&token=${TOKEN}`);
  }

  it("reads the link from the fragment and removes the token from the address bar", async () => {
    expect(readResetLink(`#email=nurse%40clinic.test&token=${TOKEN}`)).toEqual({ email: "nurse@clinic.test", token: TOKEN });
    openLink();
    renderPage(ResetPasswordPage);
    expect(await screen.findByText("For nurse@clinic.test. You will be signed out on every device.")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain(TOKEN);
  });

  it("sends the token, the password and the code, then says to sign in again", async () => {
    const post = vi.spyOn(api, "POST").mockResolvedValue(answer(204));
    openLink();
    renderPage(ResetPasswordPage);
    await userEvent.type(await screen.findByLabelText("Password"), "a brand new pass phrase");
    await userEvent.type(screen.getByLabelText("Type the password again"), "a brand new pass phrase");
    await userEvent.type(screen.getByLabelText("Authenticator code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Save new password" }));
    expect(await screen.findByText(/every device is signed out/)).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith("/api/v1/auth/password-reset/confirm", {
      body: { email: "nurse@clinic.test", token: TOKEN, password: "a brand new pass phrase", code: "123456" },
    });
  });

  it("sends no code when the field is empty, and marks it when the account needs one", async () => {
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValue(answer(400, { code: "authenticator-code-required", detail: "x" }));
    openLink();
    renderPage(ResetPasswordPage);
    await userEvent.type(await screen.findByLabelText("Password"), "a brand new pass phrase");
    await userEvent.type(screen.getByLabelText("Type the password again"), "a brand new pass phrase");
    await userEvent.click(screen.getByRole("button", { name: "Save new password" }));
    expect(await screen.findByText("Type the current code from your authenticator app.")).toBeInTheDocument();
    expect(post.mock.calls[0]?.[1]).toMatchObject({ body: { code: null } });
  });

  it("offers a new link when the link or code is not valid", async () => {
    vi.spyOn(api, "POST").mockResolvedValue(answer(400, { code: "invalid-reset-link", detail: "x" }));
    openLink();
    renderPage(ResetPasswordPage);
    await userEvent.type(await screen.findByLabelText("Password"), "a brand new pass phrase");
    await userEvent.type(screen.getByLabelText("Type the password again"), "a brand new pass phrase");
    await userEvent.click(screen.getByRole("button", { name: "Save new password" }));
    expect(await screen.findByText("This reset link or code is not valid. Ask for a new link.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ask for a new link" })).toHaveAttribute("href", "/forgot-password");
  });

  it("asks for the email link when opened without one", async () => {
    renderPage(ResetPasswordPage);
    expect(await screen.findByText("Open this page from the link in the reset email.")).toBeInTheDocument();
  });
});

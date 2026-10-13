import { expect, type Page, test } from "@playwright/test";

import { emailCode, fixture, signIn } from "../../e2e/support";

interface DemoLogin {
  role: string;
  email: string;
  clinic: string;
}

function demoUser(role: string, clinic = "a"): DemoLogin {
  const all = (fixture() as unknown as { all: DemoLogin[] }).all;
  const found = all.find((u) => u.role === role && u.clinic === clinic);
  if (!found) {
    throw new Error(`no ${role} in clinic ${clinic}`);
  }
  return found;
}

async function patientSignIn(page: Page, email: string): Promise<void> {
  await page.goto("/patient/login");
  await page.getByLabel("Email").fill(email);
  const since = Date.now() - 2000;
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByRole("link", { name: /dev mailbox/ })).toBeVisible();
  let code: string | null = null;
  await expect.poll(async () => (code = await emailCode(email, since)), { timeout: 15_000 }).not.toBeNull();
  await page.getByLabel("6 digit code").fill(code ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /^Hello, / })).toBeVisible();
}

test("flow 9: the audit page shows the checkpoint, every witness and consistency OK", async ({ page }) => {
  await signIn(page, "clinic_admin");
  await page.getByRole("tab", { name: "Audit log" }).click();
  await expect(page.getByTestId("check-chain")).toHaveText("Pass");
  await expect(page.getByTestId("check-consistency")).toHaveText("Pass");
  await expect(page.getByText(/events, signed/)).toBeVisible();

  const chain = page.getByTestId("backend-chain");
  await expect(chain).toContainText("Local anvil chain");
  await expect(chain).toContainText("Confirmed");
  await expect(chain).toContainText(/Transaction: 0x/);
  const witness = page.getByTestId("backend-witness");
  await expect(witness).toContainText("Local witness folder");
  await expect(witness).toContainText("Confirmed");
  await expect(witness).toContainText(/Commit: [0-9a-f]{10}/);
  const ots = page.getByTestId("backend-ots");
  await expect(ots).toContainText("OpenTimestamps");
  await expect(ots).toContainText("Not configured");
});

test("the seeded alert queue and break-glass queue are not empty", async ({ page }) => {
  await signIn(page, "clinic_admin");
  await expect(page.getByTestId("alert").first()).toBeVisible();
  await page.getByRole("tab", { name: "Emergency access" }).click();
  await expect(page.getByRole("article").first()).toBeVisible();
});

test("a patient verifies an access receipt against the local chain", async ({ page }) => {
  await patientSignIn(page, demoUser("patient").email);
  await page.getByRole("tab", { name: "Who opened my record" }).click();
  // The oldest entries are in a checkpoint anchored when the demo started.
  const entries = page.getByTestId("access-entry");
  await expect(entries.first()).toBeVisible();
  const oldest = entries.last();
  await oldest.getByRole("button", { name: "Check receipt" }).click();
  const result = oldest.getByTestId("receipt-result");
  await expect(result.getByRole("status")).toHaveText("Verified. This access is in the clinic's signed log.");
  await expect(result.getByRole("listitem").filter({ hasText: "Log fingerprint is on the public chain" })).toContainText("Pass");
  await expect(result.getByRole("listitem").filter({ hasText: "Clinic key is registered on chain" })).toContainText("Pass");
});

test("the dev mailbox shows the sign-in codes", async ({ page }) => {
  const email = demoUser("patient", "b").email;
  await page.request.post("/api/v1/auth/otp/request", { data: { email } });
  await page.goto("/dev/mailbox");
  await page.getByLabel("Show only emails to").fill(email);
  await expect(page.getByTestId("mail").first()).toContainText(email);
});

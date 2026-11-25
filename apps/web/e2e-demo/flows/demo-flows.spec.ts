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
  await expect.poll(async () => (code = await emailCode(email, since)), { timeout: process.env.E2E_NATIVE ? 60_000 : 15_000 }).not.toBeNull();
  await page.getByLabel("6 digit code").fill(code ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /^Hello, / })).toBeVisible();
}

test("flow 9: the audit page shows the checkpoint, every witness and consistency OK", async ({ page }) => {
  // Room for the witness poll below (up to 3 minutes).
  test.setTimeout(process.env.E2E_NATIVE ? 900_000 : 240_000);
  await signIn(page, "clinic_admin");
  await page.getByRole("tab", { name: "Audit log" }).click();
  await expect(page.getByTestId("check-chain")).toHaveText("Pass");
  await expect(page.getByTestId("check-consistency")).toHaveText("Pass");
  await expect(page.getByTestId("check-witness")).toHaveText("Pass");
  await expect(page.getByText(/events, signed/)).toBeVisible();

  const chain = page.getByTestId("backend-chain");
  await expect(chain).toContainText("Local anvil chain");
  await expect(chain).toContainText("Confirmed");
  await expect(chain).toContainText(/Transaction: 0x/);
  // The page can load while an anchor run is between the chain and the witness: the latest
  // checkpoint then has no witness receipt yet and the row says "Waiting for the next run".
  // Reload until the witness has confirmed it, for at most one anchor interval (2 minutes in
  // the demo) plus a margin, and require the witness's confirmed size to be the checkpoint size
  // the page shows, so a witness that falls behind still fails.
  const witness = page.getByTestId("backend-witness");
  await expect(async () => {
    await page.reload();
    await page.getByRole("tab", { name: "Audit log" }).click();
    await expect(witness).toContainText("Confirmed", { timeout: 5_000 });
    const signed = /(\d+) events, signed/.exec(await page.getByText(/events, signed/).innerText());
    const confirmed = /at (\d+) events/.exec(await witness.innerText());
    expect(signed?.[1]).toBeTruthy();
    expect(confirmed?.[1]).toBe(signed?.[1]);
  }).toPass({ timeout: 180_000, intervals: [5_000] });
  await expect(witness).toContainText("Local witness folder");
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
  // The oldest entries are in a checkpoint anchored when the demo started. The log is paged,
  // and a demo that has run the flows before shows only recent accesses first, so load every
  // page and check the oldest entry of all.
  const entries = page.getByTestId("access-entry");
  await expect(entries.first()).toBeVisible();
  const more = page.getByRole("button", { name: "Show more" });
  for (let pages = 0; pages < 200 && (await more.isVisible()); pages++) {
    const shown = await entries.count();
    await more.click();
    await expect.poll(() => entries.count()).toBeGreaterThan(shown);
  }
  await expect(more).toBeHidden();
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

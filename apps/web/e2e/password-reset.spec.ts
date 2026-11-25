import { expect, type Page, test } from "@playwright/test";

import { fixture, inviteTokens, resetLinks, signIn, tx } from "./support";

const FIRST_PASSWORD = "a long enough pass phrase";
const NEW_PASSWORD = "a brand new pass phrase 42";

async function signInWith(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/login");
  await page.locator("#email").fill(email);
  await page.locator("#password").fill(password);
  await page.locator("form button[type=submit]").click();
}

async function askForLink(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("link", { name: tx("auth.forgotLink") }).click();
  await expect(page).toHaveURL(/\/forgot-password$/);
  await page.locator("#reset-email").fill(email);
  await page.getByRole("button", { name: tx("reset.send") }).click();
  await expect(page.getByText(tx("reset.sent"))).toBeVisible();
}

// ADR 0010. A freshly invited person is used, so the seeded logins stay as they are.
// Routes: POST /api/v1/auth/password-reset/request, POST /api/v1/auth/password-reset/confirm.
test("staff password reset: a new password from the emailed link, and the authenticator is still asked", async ({ page }) => {
  const stamp = Date.now().toString().slice(-6);
  const domain = fixture().users[0]?.email.split("@")[1] ?? "invite.test";
  const email = `reset.nurse.${stamp}@${domain}`;
  const since = Date.now() - 2000;

  // A new staff member sets a first password from an invite.
  await signIn(page, "clinic_admin");
  await page.getByRole("tab", { name: tx("admin.tabs.staff") }).click();
  await page.locator("#inv-name").fill(`Reset Nurse ${stamp}`);
  await page.locator("#inv-email").fill(email);
  await page.locator("#inv-role").selectOption("nurse");
  await page.getByRole("button", { name: tx("admin.sendInvite") }).click();
  await expect(page.getByText(tx("admin.invited"))).toBeVisible();
  let invites: string[] = [];
  await expect.poll(async () => (invites = await inviteTokens(email, since)).length, { timeout: 15_000 }).toBe(1);
  await page.goto(`/invite?email=${encodeURIComponent(email)}&token=${encodeURIComponent(invites[0] ?? "")}`);
  await page.locator("#new-password").fill(FIRST_PASSWORD);
  await page.locator("#repeat-password").fill(FIRST_PASSWORD);
  await page.getByRole("button", { name: tx("invite.submit") }).click();
  await expect(page.getByText(tx("invite.done"))).toBeVisible();

  // An email with no account gets the same message, and no link.
  const nobody = `nobody.${stamp}@${domain}`;
  await askForLink(page, nobody);

  // The person asks for a link from the sign-in page.
  await askForLink(page, email);
  let links: string[] = [];
  await expect.poll(async () => (links = await resetLinks(email, since)).length, { timeout: 15_000 }).toBe(1);
  expect(await resetLinks(nobody, since)).toEqual([]);

  // The page takes the token out of the address bar and fits a 360 px screen.
  await page.goto(links[0] ?? "");
  await expect(page.getByText(tx("reset.subtitle", { email }))).toBeVisible();
  await expect(page).toHaveURL(/\/reset-password$/);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  // No authenticator yet, so the code field stays empty.
  await page.locator("#reset-password").fill(NEW_PASSWORD);
  await page.locator("#reset-repeat").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: tx("reset.submit") }).click();
  await expect(page.getByText(tx("reset.done"))).toBeVisible();

  // The link works once. (Load another page first: a link that only differs after "#" would
  // not reload the page.)
  await page.goto("/login");
  await page.goto(links[0] ?? "");
  await page.locator("#reset-password").fill(`${NEW_PASSWORD} again`);
  await page.locator("#reset-repeat").fill(`${NEW_PASSWORD} again`);
  await page.getByRole("button", { name: tx("reset.submit") }).click();
  await expect(page.getByText(tx("errors.invalid-reset-link"))).toBeVisible();

  // The old password stops working; the new one leads to the authenticator, not into the app.
  await signInWith(page, email, FIRST_PASSWORD);
  await expect(page.getByText(tx("errors.invalid-credentials"))).toBeVisible();
  await signInWith(page, email, NEW_PASSWORD);
  await expect(page).toHaveURL(/\/login\/enrol/);
});

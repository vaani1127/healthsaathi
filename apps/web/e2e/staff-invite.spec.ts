import { expect, type Page, test } from "@playwright/test";

import { fixture, inviteTokens, signIn, tx } from "./support";

const PASSWORD = "a long enough pass phrase";

async function setPassword(page: Page, email: string, token: string): Promise<void> {
  await page.goto(`/invite?email=${encodeURIComponent(email)}&token=${encodeURIComponent(token)}`);
  await page.locator("#new-password").fill(PASSWORD);
  await page.locator("#repeat-password").fill(PASSWORD);
  await page.getByRole("button", { name: tx("invite.submit") }).click();
}

// APP_GAPS 35: a clinic admin sends an invite again; only the newest link works.
test("staff invite: the admin sends it again and only the new link sets a password", async ({ page }) => {
  const stamp = Date.now().toString().slice(-6);
  const name = `Invited Nurse ${stamp}`;
  const email = `invited.nurse.${stamp}@${fixture().users[0]?.email.split("@")[1] ?? "invite.test"}`;
  const since = Date.now() - 2000;

  await signIn(page, "clinic_admin");
  await page.getByRole("tab", { name: tx("admin.tabs.staff") }).click();
  await page.locator("#inv-name").fill(name);
  await page.locator("#inv-email").fill(email);
  await page.locator("#inv-role").selectOption("nurse");
  await page.getByRole("button", { name: tx("admin.sendInvite") }).click();
  await expect(page.getByText(tx("admin.invited"))).toBeVisible();

  const row = page.getByRole("listitem").filter({ hasText: email });
  await expect(row.getByText(tx("admin.noPassword"))).toBeVisible();
  await row.getByRole("button", { name: tx("admin.inviteAgain") }).click();
  await expect(page.getByTestId("invite-again-confirmation")).toContainText(tx("admin.invitedAgain", { email }));

  let tokens: string[] = [];
  await expect.poll(async () => (tokens = await inviteTokens(email, since)).length, { timeout: 15_000 }).toBe(2);
  const [first, second] = tokens as [string, string];

  // The first link no longer works.
  await setPassword(page, email, first);
  await expect(page.getByRole("alert")).toHaveText(tx("errors.invalid-invite"));

  // The new link sets the password, and signing in leads to the authenticator setup.
  await setPassword(page, email, second);
  await expect(page.getByText(tx("invite.done"))).toBeVisible();
  await page.goto("/login");
  await page.locator("#email").fill(email);
  await page.locator("#password").fill(PASSWORD);
  await page.locator("form button[type=submit]").click();
  await expect(page).toHaveURL(/\/login\/enrol/);
});

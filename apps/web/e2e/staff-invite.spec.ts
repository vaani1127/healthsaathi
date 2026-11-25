import { expect, type Page, test } from "@playwright/test";

import { fixture, inviteTokens, signIn, tx, user } from "./support";

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

// AUDIT_REPORT m4: deactivating staff and pausing a schedule or service is confirmed first, and
// the dialog says it can be undone. Names are matched exactly: "Activate" is part of "Deactivate". Everything is turned back on at the end.
test("admin confirms before deactivating staff or pausing a schedule or service", async ({ page }) => {
  await signIn(page, "clinic_admin");
  const dialog = page.getByRole("dialog");
  const undo = tx("confirm.undoable", { undo: tx("admin.activate") });

  await page.getByRole("tab", { name: tx("admin.tabs.staff") }).click();
  const member = page.getByRole("listitem").filter({ hasText: user("nurse").email });
  await member.getByRole("button", { name: tx("admin.deactivate"), exact: true }).click();
  await expect(dialog).toContainText(tx("confirm.deactivateTitle", { name: user("nurse").name }));
  await expect(dialog).toContainText(undo);
  await dialog.getByRole("button", { name: tx("confirm.cancel") }).click();
  await expect(member.getByRole("button", { name: tx("admin.deactivate"), exact: true })).toBeVisible();
  await member.getByRole("button", { name: tx("admin.deactivate"), exact: true }).click();
  await dialog.getByRole("button", { name: tx("confirm.deactivate") }).click();
  await member.getByRole("button", { name: tx("admin.activate"), exact: true }).click();
  await expect(member.getByRole("button", { name: tx("admin.deactivate"), exact: true })).toBeVisible();

  await page.getByRole("tab", { name: tx("admin.tabs.schedules") }).click();
  const schedule = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: tx("admin.pause") }) }).first();
  const scheduleText = (await schedule.innerText()).split("\n")[0] ?? "";
  await schedule.getByRole("button", { name: tx("admin.pause") }).click();
  await expect(dialog).toContainText(tx("confirm.pauseScheduleTitle"));
  await expect(dialog).toContainText(undo);
  await dialog.getByRole("button", { name: tx("confirm.pause") }).click();
  const paused = page.getByRole("listitem").filter({ hasText: scheduleText }).filter({ has: page.getByRole("button", { name: tx("admin.activate"), exact: true }) }).first();
  await paused.getByRole("button", { name: tx("admin.activate"), exact: true }).click();
  await expect(page.getByRole("listitem").filter({ hasText: scheduleText }).getByRole("button", { name: tx("admin.pause") }).first()).toBeVisible();

  await page.getByRole("tab", { name: tx("admin.tabs.services") }).click();
  const service = page.getByRole("listitem").filter({ hasText: "Consultation" }).filter({ has: page.getByRole("button", { name: tx("admin.pause") }) }).first();
  await service.getByRole("button", { name: tx("admin.pause") }).click();
  await expect(dialog).toContainText(tx("confirm.pauseServiceTitle"));
  await expect(dialog).toContainText(undo);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await service.getByRole("button", { name: tx("admin.pause") }).click();
  await dialog.getByRole("button", { name: tx("confirm.pause") }).click();
  const pausedService = page.getByRole("listitem").filter({ hasText: "Consultation" }).filter({ has: page.getByRole("button", { name: tx("admin.activate"), exact: true }) }).first();
  await pausedService.getByRole("button", { name: tx("admin.activate"), exact: true }).click();
  await expect(pausedService).toBeHidden();
});

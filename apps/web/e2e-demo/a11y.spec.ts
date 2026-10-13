import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { emailCode, fixture, signIn } from "../e2e/support";

// P18 accessibility pass: axe-core (WCAG 2.0 and 2.1, levels A and AA) on every role's screens,
// every tab, in English and Hindi. Fails on any violation.

async function axe(page: Page, where: string): Promise<void> {
  await page.waitForLoadState("networkidle");
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const found = result.violations.map(
    (v) => `${where}: ${v.id} (${v.impact ?? "?"}) ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`,
  );
  expect(found).toEqual([]);
}

async function everyTab(page: Page, where: string): Promise<void> {
  await axe(page, where);
  const tabs = page.getByRole("tab");
  for (let i = 0; i < (await tabs.count()); i++) {
    await tabs.nth(i).click();
    await axe(page, `${where} tab ${i + 1}`);
  }
}

for (const lang of ["en", "hi"]) {
  test.describe(lang, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript((l) => localStorage.setItem("hs.lang", l), lang);
    });

    test("public pages", async ({ page }) => {
      for (const path of ["/login", "/patient/login", "/verify", "/dev/mailbox"]) {
        await page.goto(path);
        await axe(page, path);
      }
    });

    for (const role of ["clinic_admin", "reception", "nurse", "doctor", "lab_tech"]) {
      test(role, async ({ page }) => {
        await signIn(page, role);
        await everyTab(page, role);
        const first = page.locator('a[href*="/patients/"]').first();
        if (await first.count()) {
          await first.click();
          await axe(page, `${role} patient page`);
        }
      });
    }

    test("patient", async ({ page }) => {
      const all = (fixture() as unknown as { all: { role: string; email: string; clinic: string }[] }).all;
      const email = all.find((u) => u.role === "patient" && u.clinic === "b")?.email ?? "";
      await page.goto("/patient/login");
      await page.locator("#p-email").fill(email);
      const since = Date.now() - 2000;
      await page.locator("form button[type=submit]").click();
      let code: string | null = null;
      await expect.poll(async () => (code = await emailCode(email, since)), { timeout: process.env.E2E_NATIVE ? 60_000 : 15_000 }).not.toBeNull();
      await page.locator("#p-code").fill(code ?? "");
      await page.locator("form button[type=submit]").click();
      await expect(page).toHaveURL(/\/patient/);
      await everyTab(page, "patient");
    });
  });
}

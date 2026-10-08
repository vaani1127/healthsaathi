import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { expect, type Page, test } from "@playwright/test";

import { signIn, user } from "./support";

const API_DIR = fileURLToPath(new URL("../../api", import.meta.url));
// An empty model folder, so detection uses the rules baseline whatever was fitted locally.
const MODELS = fileURLToPath(new URL("../test-results/models", import.meta.url));

/** Reception registers a walk-in patient and returns the new patient's id. */
async function register(page: Page, name: string): Promise<string> {
  await signIn(page, "reception");
  await page.getByRole("button", { name: "Register new patient" }).click();
  await page.getByLabel("Full name").fill(name);
  await page.getByLabel("Sex").selectOption("male");
  await page.getByLabel("Phone").fill("+91-00000-54321");
  await page.getByLabel("I read this notice to the patient and the patient agreed.").check();
  await page.getByRole("button", { name: "Register", exact: true }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  const id = /\/reception\/patients\/([0-9a-f-]+)/.exec(page.url())?.[1];
  expect(id).toBeTruthy();
  return id as string;
}

// SPEC 1.3 flows 7 and 8.
test.describe.serial("review", () => {
  const stamp = Date.now().toString().slice(-6);
  const emergencyName = `Rakesh Glass ${stamp}`;
  const reasonName = `Mohan Reason ${stamp}`;
  let emergencyId = "";
  let reasonId = "";

  test("flow 7: doctor uses break-glass with a reason", async ({ page }) => {
    emergencyId = await register(page, emergencyName);
    await signIn(page, "doctor");
    await page.goto(`/doctor/patients/${emergencyId}`);
    await expect(page.getByText("Reason needed")).toBeVisible();
    await page.getByRole("button", { name: "Break glass" }).click();
    await page.locator("#bg-code").selectOption("unconscious");
    await page.locator("#bg-text").fill("Collapsed in the waiting area, not responding");
    await page.getByRole("button", { name: "Open emergency view" }).click();
    await expect(page.getByText(`Emergency view: ${emergencyName}`)).toBeVisible();
  });

  test("flow 7: admin sees it in the 24-hour review queue and reviews it", async ({ page }) => {
    await signIn(page, "clinic_admin");
    await page.getByRole("tab", { name: "Emergency access" }).click();
    const item = page.getByRole("article").filter({ hasText: emergencyName });
    await expect(item).toContainText(user("doctor").name);
    await expect(item).toContainText("Patient unconscious");
    await item.getByRole("button", { name: "Justified" }).click();
    await expect(item.getByText("Justified")).toBeVisible();
  });

  test("flow 8: a doctor opens a record with only a typed reason", async ({ page }) => {
    reasonId = await register(page, reasonName);
    await signIn(page, "doctor");
    await page.goto(`/doctor/patients/${reasonId}`);
    await page.locator("#reason-code").selectOption("second_opinion");
    await page.locator("#reason-text").fill("Asked to look at this case");
    await page.getByRole("button", { name: "Open record" }).click();
    await expect(page.getByRole("heading", { name: reasonName })).toBeVisible();
  });

  test("flow 8: admin reviews the top alert with its explanation and features", async ({ page }) => {
    execFileSync("uv", ["run", "python", "-m", "app.detect.job"], {
      cwd: API_DIR,
      env: { ...process.env, MODEL_DIR: MODELS },
      stdio: "inherit",
    });
    await signIn(page, "clinic_admin");
    await expect(page.getByRole("tab", { name: "Alerts" })).toHaveAttribute("aria-selected", "true");
    // The reason-only access was gated and scored; the queue shows today's top alerts.
    await expect(page.getByTestId("alert")).not.toHaveCount(0);
    const first = page.getByTestId("alert").first();
    await expect(first).toContainText("#1");
    await expect(first).toContainText("Score");
    await first.getByRole("button", { name: "Open" }).click();
    const detail = first.getByTestId("alert-detail");
    await expect(detail).toContainText("Patient:");
    await expect(detail).toContainText("What stands out");
    await expect(detail).toContainText("This user's last accesses");
    await expect(detail).toContainText("Scored by rules");
    await detail.getByLabel("Note (optional)").fill("Not asked for a second opinion");
    await detail.getByRole("button", { name: "Misuse" }).click();
    await expect(detail.getByRole("status")).toHaveText("Reviewed: Misuse");
    await expect(first).toContainText("Misuse");
  });
});

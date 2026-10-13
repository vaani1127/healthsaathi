import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { expect, type Page, test } from "@playwright/test";

import { DEMO, signIn, tx, user } from "./support";

const API_DIR = fileURLToPath(new URL("../../api", import.meta.url));
// An empty model folder, so detection uses the rules baseline whatever was fitted locally.
const MODELS = fileURLToPath(new URL("../test-results/models", import.meta.url));

/** Reception registers a walk-in patient and returns the new patient's id. */
async function register(page: Page, name: string): Promise<string> {
  await signIn(page, "reception");
  await page.getByRole("button", { name: tx("reception.registerNew") }).click();
  await page.getByLabel(tx("patient.name")).fill(name);
  await page.getByLabel(tx("patient.sex")).selectOption("male");
  await page.getByLabel(tx("patient.phone")).fill("+91-00000-54321");
  await page.getByLabel(tx("consent.agreed")).check();
  await page.getByRole("button", { name: tx("reception.register"), exact: true }).click();
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
    await expect(page.getByText(tx("reason.title"))).toBeVisible();
    await page.getByRole("button", { name: tx("breakGlass.start") }).click();
    await page.locator("#bg-code").selectOption("unconscious");
    await page.locator("#bg-text").fill("Collapsed in the waiting area, not responding");
    await page.getByRole("button", { name: tx("breakGlass.confirm") }).click();
    await expect(page.getByText(tx("breakGlass.viewTitle", { name: emergencyName }))).toBeVisible();
  });

  test("flow 7: admin sees it in the 24-hour review queue and reviews it", async ({ page }) => {
    await signIn(page, "clinic_admin");
    await page.getByRole("tab", { name: tx("admin.tabs.breakGlass") }).click();
    const item = page.getByRole("article").filter({ hasText: emergencyName });
    await expect(item).toContainText(user("doctor").name);
    await expect(item).toContainText(tx("breakGlass.codes.unconscious"));
    await item.getByRole("button", { name: tx("outcome.benign") }).click();
    await expect(item.getByText(tx("outcome.benign"))).toBeVisible();
  });

  test("flow 8: a doctor opens a record with only a typed reason", async ({ page }) => {
    reasonId = await register(page, reasonName);
    await signIn(page, "doctor");
    await page.goto(`/doctor/patients/${reasonId}`);
    await page.locator("#reason-code").selectOption("second_opinion");
    await page.locator("#reason-text").fill("Asked to look at this case");
    await page.getByRole("button", { name: tx("reason.open") }).click();
    await expect(page.getByRole("heading", { name: reasonName })).toBeVisible();
  });

  test("flow 8: admin reviews the top alert with its explanation and features", async ({ page }) => {
    if (DEMO) {
      execFileSync("docker", ["compose", "-f", "../../infra/docker-compose.demo.yml", "exec", "-T", "api", "demo-entrypoint", "python", "-m", "app.detect.job"], {
        stdio: "inherit",
      });
    } else {
      execFileSync("uv", ["run", "python", "-m", "app.detect.job"], {
        cwd: API_DIR,
        env: { ...process.env, MODEL_DIR: MODELS },
        stdio: "inherit",
      });
    }
    await signIn(page, "clinic_admin");
    await expect(page.getByRole("tab", { name: tx("admin.tabs.alerts") })).toHaveAttribute("aria-selected", "true");
    // The reason-only access was gated and scored; the queue shows today's top alerts.
    await expect(page.getByTestId("alert")).not.toHaveCount(0);
    const first = page.getByTestId("alert").first();
    await expect(first).toContainText(/#\d+/);
    await expect(first).toContainText(tx("admin.alerts.score"));
    await first.getByRole("button", { name: tx("admin.alerts.open") }).click();
    const detail = first.getByTestId("alert-detail");
    await expect(detail).toContainText(`${tx("admin.alerts.patient")}:`);
    await expect(detail).toContainText(tx("admin.alerts.topFeatures"));
    await expect(detail).toContainText(tx("admin.alerts.timeline"));
    await expect(detail).toContainText(DEMO ? tx("admin.alerts.scorer", { scorer: "" }).trim() : tx("admin.alerts.scorer", { scorer: "rules" }));
    await detail.getByLabel(tx("admin.alerts.note")).fill("Not asked for a second opinion");
    await detail.getByRole("button", { name: tx("outcome.misuse") }).click();
    await expect(detail.getByRole("status")).toHaveText(tx("admin.alerts.reviewed", { outcome: tx("outcome.misuse") }));
    // A reviewed alert leaves the open queue; the day view still lists it with its outcome.
    await page.getByRole("button", { name: tx("admin.alerts.view.day") }).click();
    await expect(page.getByTestId("alert").filter({ hasText: tx("admin.alerts.status.misuse") }).first()).toBeVisible();
  });
});

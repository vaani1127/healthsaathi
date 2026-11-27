import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { signIn, tx, user } from "../e2e/support";

// The record tamper demo (ADR 0011): a nurse records vitals, the anchor job runs, and the record
// checks out in the browser. Then the database owner changes the reading (with the append-only
// trigger off, as only the owner can), the browser check fails, and the reading is put back.

const COMPOSE = ["compose", "-f", "../../infra/docker-compose.demo.yml"];
const ROOT = fileURLToPath(new URL("../../..", import.meta.url));

function sql(statement: string): string {
  if (process.env.E2E_NATIVE) {
    return execFileSync("uv", ["run", "python", "-m", "hs_demo.native", "sql", statement], {
      cwd: ROOT,
      encoding: "utf-8",
    }).trim();
  }
  return execFileSync(
    "docker",
    [...COMPOSE, "exec", "-T", "postgres", "psql", "-U", "healthsaathi", "-d", "healthsaathi", "-tAc", statement],
    { encoding: "utf-8", env: { ...process.env, MSYS_NO_PATHCONV: "1" } },
  ).trim();
}

function runAnchorJob(): void {
  if (process.env.E2E_NATIVE) {
    execFileSync("uv", ["run", "python", "-m", "hs_demo.native", "run", "app.anchor.job"], { cwd: ROOT });
    return;
  }
  execFileSync("docker", [...COMPOSE, "exec", "-T", "api", "python", "-m", "app.anchor.job"], {
    env: { ...process.env, MSYS_NO_PATHCONV: "1" },
  });
}

function setPulse(vitalId: string, pulse: number): void {
  sql(
    "BEGIN; ALTER TABLE vitals DISABLE TRIGGER vitals_append_only; " +
      `UPDATE vitals SET pulse = ${pulse} WHERE id = '${vitalId}'; ` +
      "ALTER TABLE vitals ENABLE TRIGGER vitals_append_only; COMMIT;",
  );
}

test("a record checks out, fails after a database edit, and checks out again", async ({ page }) => {
  const nurse = user("nurse");
  const patientId = sql(
    "SELECT p.id FROM patients p JOIN memberships m ON m.clinic_id = p.clinic_id " +
      `JOIN users u ON u.id = m.user_id WHERE u.email = '${nurse.email}' ORDER BY p.created_at LIMIT 1`,
  );
  expect(patientId).toMatch(/^[0-9a-f-]{36}$/);

  await signIn(page, "nurse");
  await page.goto(`/app/nurse/patients/${patientId}`);
  await page.getByLabel(tx("vitals.pulse")).fill("81");
  await page.getByRole("button", { name: tx("vitals.save") }).click();
  await expect(page.getByText(tx("vitals.saved"))).toBeVisible();

  const vitalId = sql(`SELECT id FROM vitals WHERE patient_id = '${patientId}' ORDER BY created_at DESC LIMIT 1`);
  runAnchorJob();

  const newest = page.getByTestId("vitals-list").getByRole("listitem").first();
  await newest.getByRole("button", { name: tx("record.checkThis") }).click();
  const result = newest.getByTestId("record-result");
  await expect(result).toHaveAttribute("data-verdict", "verified");
  await expect(result.getByRole("status")).toHaveText(tx("record.verdict.verified"));

  setPulse(vitalId, 140);
  try {
    await newest.getByRole("button", { name: tx("record.again") }).click();
    await expect(result).toHaveAttribute("data-verdict", "failed");
    await expect(result.getByRole("status")).toHaveText(tx("record.verdict.failed"));
  } finally {
    setPulse(vitalId, 81);
  }
  await newest.getByRole("button", { name: tx("record.again") }).click();
  await expect(result).toHaveAttribute("data-verdict", "verified");
});

import { execFileSync } from "node:child_process";

import { expect, test } from "@playwright/test";

import { signIn, user } from "../e2e/support";

// The tamper demo: someone with database owner rights edits one audit row. The app role cannot
// do this (INSERT only), so the test uses the postgres superuser inside the demo stack, turns the
// append-only trigger off for the edit, and afterwards puts the row back exactly as it was.

const COMPOSE = ["compose", "-f", "../../infra/docker-compose.demo.yml"];

function sql(statement: string): string {
  return execFileSync(
    "docker",
    [...COMPOSE, "exec", "-T", "postgres", "psql", "-U", "healthsaathi", "-d", "healthsaathi", "-tAc", statement],
    { encoding: "utf-8", env: { ...process.env, MSYS_NO_PATHCONV: "1" } },
  ).trim();
}

function rowOfAdminClinic(email: string): string {
  return sql(
    "SELECT min(a.seq) FROM audit_events a JOIN memberships m ON m.clinic_id = a.clinic_id " +
      `JOIN users u ON u.id = m.user_id WHERE u.email = '${email}'`,
  );
}

function edit(seq: string, change: string): void {
  sql(
    "BEGIN; ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only; " +
      `UPDATE audit_events SET payload = ${change} WHERE seq = ${seq}; ` +
      "ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only; COMMIT;",
  );
}

test("an edited audit row makes the audit page show FAIL", async ({ page }) => {
  const admin = user("clinic_admin");
  const seq = rowOfAdminClinic(admin.email);
  expect(seq).toMatch(/^\d+$/);

  await signIn(page, "clinic_admin");
  await page.getByRole("tab", { name: "Audit log" }).click();
  await expect(page.getByTestId("check-chain")).toHaveText("Pass");

  edit(seq, `payload || '{"tampered": true}'::jsonb`);
  try {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByTestId("check-chain")).toHaveText("Fail");
    await expect(page.getByTestId("audit-problem")).toContainText("FAIL: the audit log hash chain is broken");
  } finally {
    edit(seq, "payload - 'tampered'");
  }
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.getByTestId("check-chain")).toHaveText("Pass");
});

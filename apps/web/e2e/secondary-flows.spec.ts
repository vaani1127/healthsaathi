import { expect, type Page, test } from "@playwright/test";
import { TOTP } from "otpauth";

import { emailCode, fixture, inviteTokens, signIn, tx, user } from "./support";

// Browser coverage for the routes no browser run used in the full app audit (AUDIT_REPORT,
// "Untested"). Clinic A of the run's fixture, forms filled in as staff would. Every step names the
// routes it covers in a comment, so infra/audit/inventory.py can map them. Button names are
// matched exactly ("Activate" is part of "Deactivate").

const PASSWORD = "a long enough pass phrase";
const NEW_PASSWORD = "another long pass phrase";
const TEST_MARK = "TEST VERSION (secondary flows spec).";

async function register(page: Page, name: string, email: string): Promise<string> {
  await page.getByRole("button", { name: tx("reception.registerNew"), exact: true }).click();
  await page.getByLabel(tx("patient.name")).fill(name);
  await page.getByLabel(tx("patient.sex")).selectOption("female");
  await page.getByLabel(tx("patient.phone")).fill("+91-00000-67890");
  await page.getByLabel(tx("consent.agreed")).check();
  await page.getByRole("button", { name: tx("reception.register"), exact: true }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  const id = /\/reception\/patients\/([0-9a-f-]+)/.exec(page.url())?.[1] ?? "";
  expect(id).toBeTruthy();
  if (email) {
    await page.getByLabel(tx("auth.email"), { exact: true }).fill(email);
    await page.getByRole("button", { name: tx("reception.portalGrant"), exact: true }).click();
    await expect(page.getByText(tx("reception.portalSent"))).toBeVisible();
  }
  return id;
}

test.describe.serial("secondary flows", () => {
  const stamp = Date.now().toString().slice(-6);
  const domain = fixture().users[0]?.email.split("@")[1] ?? "e2e.healthsaathi.test";
  const doctor2 = { name: `Second Doctor ${stamp}`, email: `second.doctor.${stamp}@${domain}`, secret: "" };
  const patientName = `Asha Second ${stamp}`;
  const patientEmail = `asha.second.${stamp}@${domain}`;
  const emergencyName = `Ravi Glass ${stamp}`;
  let patientId = "";
  let emergencyId = "";

  // POST /api/v1/auth/totp/activate, POST /api/v1/auth/password
  test("a newly invited doctor sets up the authenticator and changes their own password", async ({ page, browser }) => {
    const since = Date.now() - 2000;
    await signIn(page, "clinic_admin");
    await page.getByRole("tab", { name: tx("admin.tabs.staff"), exact: true }).click();
    await page.locator("#inv-name").fill(doctor2.name);
    await page.locator("#inv-email").fill(doctor2.email);
    await page.locator("#inv-role").selectOption("doctor");
    await page.getByRole("button", { name: tx("admin.sendInvite"), exact: true }).click();
    await expect(page.getByText(tx("admin.invited"))).toBeVisible();
    let tokens: string[] = [];
    await expect.poll(async () => (tokens = await inviteTokens(doctor2.email, since)).length, { timeout: 15_000 }).toBe(1);

    const context = await browser.newContext();
    const fresh = await context.newPage();
    await fresh.goto(`/invite?email=${encodeURIComponent(doctor2.email)}&token=${encodeURIComponent(tokens[0] ?? "")}`);
    await fresh.locator("#new-password").fill(PASSWORD);
    await fresh.locator("#repeat-password").fill(PASSWORD);
    await fresh.getByRole("button", { name: tx("invite.submit"), exact: true }).click();
    await expect(fresh.getByText(tx("invite.done"))).toBeVisible();
    await fresh.goto("/login");
    await fresh.locator("#email").fill(doctor2.email);
    await fresh.locator("#password").fill(PASSWORD);
    await fresh.locator("form button[type=submit]").click();
    await expect(fresh).toHaveURL(/\/login\/enrol/);
    doctor2.secret = (await fresh.getByTestId("totp-secret").innerText()).trim();
    await fresh.locator("#code").fill(new TOTP({ secret: doctor2.secret, digits: 6, period: 30 }).generate());
    await fresh.getByRole("button", { name: tx("auth.activate"), exact: true }).click();
    await expect(fresh).not.toHaveURL(/\/login/);

    await fresh.goto("/account/sessions");
    await fresh.locator("#current-password").fill(PASSWORD);
    await fresh.locator("#new-password").fill(NEW_PASSWORD);
    await fresh.getByRole("button", { name: tx("password.save"), exact: true }).click();
    await expect(fresh.getByText(tx("password.changed"))).toBeVisible();
    await context.close();
  });

  test("reception registers the patients and adds a walk-in", async ({ page }) => {
    await signIn(page, "reception");
    emergencyId = await register(page, emergencyName, "");
    await page.goto("/reception");
    patientId = await register(page, patientName, patientEmail);
    await page.locator("#walkin-doctor").selectOption({ label: user("doctor").name });
    await page.getByRole("button", { name: tx("reception.addWalkIn"), exact: true }).click();
    await expect(page.getByTestId("token-number")).toBeVisible();
  });

  // POST /api/v1/patients/{patient_id}/allergies, PATCH /api/v1/allergies/{allergy_id}
  test("nurse adds an allergy and marks it inactive", async ({ page }) => {
    await signIn(page, "nurse");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await page.locator("#allergy-substance").fill("Penicillin");
    await page.locator("#allergy-severity").selectOption("severe");
    await page.getByRole("button", { name: tx("allergy.add"), exact: true }).click();
    const allergy = page.getByRole("listitem").filter({ hasText: "Penicillin" });
    await allergy.getByRole("button", { name: tx("allergy.deactivate"), exact: true }).click();
    await expect(allergy.getByRole("button", { name: tx("allergy.reactivate"), exact: true })).toBeVisible();
  });

  // POST /api/v1/patients/{patient_id}/conditions, PATCH /api/v1/conditions/{condition_id},
  // PATCH /api/v1/notes/{note_id}, POST /api/v1/notes/{note_id}/versions,
  // GET /api/v1/notes/{note_id}/history, POST /api/v1/prescriptions/{rx_id}/versions,
  // POST /api/v1/care-team, POST /api/v1/care-team/{assignment_id}/end,
  // POST /api/v1/patients/{patient_id}/referrals, POST /api/v1/encounters/{encounter_id}/close
  test("doctor works the chart: condition, note versions, prescription amend, care team, referral", async ({ page }) => {
    await signIn(page, "doctor");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();

    await page.locator("#cond-text").fill("Seasonal asthma");
    await page.getByRole("button", { name: tx("conditions.add"), exact: true }).click();
    const condition = page.getByRole("listitem").filter({ hasText: "Seasonal asthma" });
    await condition.getByRole("button", { name: tx("conditions.resolve"), exact: true }).click();
    await expect(condition).toBeHidden();

    await page.getByRole("button", { name: tx("consult.start"), exact: true }).click();
    await page.locator("#note-body").fill("Cough for 3 days.");
    await page.getByRole("button", { name: tx("notes.saveDraft"), exact: true }).click();
    await page.waitForLoadState("networkidle");
    // A second save edits the same draft.
    const edited = page.waitForResponse((r) => r.request().method() === "PATCH" && /\/api\/v1\/notes\/[^/]+$/.test(r.url()));
    await page.locator("#note-body").fill("Cough for 3 days. Chest clear.");
    await page.getByRole("button", { name: tx("notes.saveDraft"), exact: true }).click();
    expect((await edited).status()).toBe(200);
    await page.getByRole("button", { name: tx("notes.sign"), exact: true }).first().click();
    await expect(page.getByText(tx("notes.signed"))).toBeVisible();

    const history = page.getByRole("article").filter({ hasText: "Chest clear." }).first();
    await history.getByRole("button", { name: tx("notes.amend"), exact: true }).click();
    // While amending, the note's text is in the amend box, so it is found by its id.
    const amendBox = page.locator('textarea[id^="amend-"]');
    await amendBox.fill("Cough for 3 days. Chest clear. Follow up in a week.");
    await page.getByRole("button", { name: tx("notes.saveVersion"), exact: true }).click();
    const latest = page.getByRole("article").filter({ hasText: "Follow up in a week." }).first();
    const versions = page.waitForResponse((r) => /\/api\/v1\/notes\/[^/]+\/history$/.test(r.url()));
    await latest.getByRole("button", { name: tx("notes.versions"), exact: true }).click();
    expect((await versions).status()).toBe(200);

    await page.getByLabel(tx("rx.drug"), { exact: true }).fill("Salbutamol inhaler");
    await page.getByLabel(tx("rx.dose")).fill("1-0-1");
    await page.getByLabel(tx("rx.days")).fill("7");
    await page.getByRole("button", { name: tx("rx.save"), exact: true }).click();
    await expect(page.getByText(tx("rx.saved"))).toBeVisible();
    const rx = page.getByRole("article").filter({ hasText: "Salbutamol inhaler" }).first();
    await rx.getByRole("button", { name: tx("rx.sign"), exact: true }).click();
    await rx.getByRole("button", { name: tx("rx.amend"), exact: true }).click();
    await page.getByLabel(tx("rx.days")).last().fill("10");
    const amended = page.waitForResponse((r) => r.request().method() === "POST" && /\/api\/v1\/prescriptions\/[^/]+\/versions$/.test(r.url()));
    await page.getByRole("button", { name: tx("rx.saveVersion"), exact: true }).click();
    expect((await amended).status()).toBe(201);

    await page.locator("#ct-member").selectOption({ label: `${doctor2.name} · ${tx("roles.doctor")}` });
    await page.getByRole("button", { name: tx("careTeam.addButton"), exact: true }).click();
    const member = page.getByRole("listitem").filter({ hasText: doctor2.name });
    await member.getByRole("button", { name: tx("careTeam.end"), exact: true }).click();
    await expect(member.getByRole("button", { name: tx("careTeam.end"), exact: true })).toBeHidden();

    await page.locator("#ref-to").selectOption({ label: doctor2.name });
    await page.locator("#ref-reason").fill("Asthma review, please.");
    await page.getByRole("button", { name: tx("referral.send"), exact: true }).click();
    await expect(page.getByText(tx("referral.sent"))).toBeVisible();

    await page.getByRole("checkbox", { name: tx("lab.tests.CBC") }).check();
    await page.getByRole("button", { name: tx("lab.placeOrder"), exact: true }).click();
    await expect(page.getByText(tx("lab.ordered"))).toBeVisible();

    await page.getByRole("button", { name: tx("consult.finish"), exact: true }).click();
    await expect(page.getByText(tx("consult.finished"))).toBeVisible();
  });

  // GET /api/v1/documents/{document_id}
  test("lab releases a result with a file and the doctor opens the file", async ({ page }) => {
    await signIn(page, "lab_tech");
    const item = page.getByTestId("work-item").filter({ hasText: patientName });
    await item.getByRole("button", { name: tx("lab.collected"), exact: true }).click();
    await item.getByLabel(tx("lab.valueFor", { test: tx("lab.tests.CBC") })).fill("12.9");
    await item.getByLabel(tx("lab.unit")).fill("g/dL");
    await item.getByLabel(tx("lab.report")).setInputFiles({ name: "cbc.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 synthetic CBC report") });
    await item.getByRole("button", { name: tx("lab.saveAndRelease"), exact: true }).click();
    await expect(page.getByText(tx("lab.released"))).toBeVisible();

    await page.getByRole("button", { name: tx("auth.signOut"), exact: true }).click();
    await signIn(page, "doctor");
    await page.goto(`/doctor/patients/${patientId}`);
    const file = page.waitForResponse((r) => /\/api\/v1\/documents\/[^/]+$/.test(r.url()));
    await page.getByRole("button", { name: tx("portal.downloadReport"), exact: true }).first().click();
    expect((await file).status()).toBe(200);
  });

  // GET /api/v1/patients/{patient_id}/emergency
  test("the break-glass emergency view reopens after a page reload", async ({ page }) => {
    await signIn(page, "doctor");
    await page.goto(`/doctor/patients/${emergencyId}`);
    await page.getByRole("button", { name: tx("breakGlass.start"), exact: true }).click();
    await page.locator("#bg-code").selectOption("unconscious");
    await page.locator("#bg-text").fill("Collapsed at the front desk, not responding");
    await page.getByRole("button", { name: tx("breakGlass.confirm"), exact: true }).click();
    const title = page.getByText(tx("breakGlass.viewTitle", { name: emergencyName }));
    await expect(title).toBeVisible();
    const reopened = page.waitForResponse((r) => /\/api\/v1\/patients\/[^/]+\/emergency/.test(r.url()));
    await page.reload();
    expect((await reopened).status()).toBe(200);
    await expect(title).toBeVisible();
  });

  // POST /api/v1/invoices/{invoice_id}/void
  test("reception voids a draft invoice", async ({ page }) => {
    await signIn(page, "reception");
    await page.goto(`/reception/patients/${patientId}`);
    await page.getByLabel(/^Consultation/).fill("1");
    await page.getByRole("button", { name: tx("billing.create"), exact: true }).click();
    await expect(page).toHaveURL(/\/reception\/invoices\//);
    // Voiding cannot be undone, so the app asks first (AUDIT_REPORT m6). Cancel keeps the draft.
    await page.getByRole("button", { name: tx("billing.void"), exact: true }).click();
    const dialog = page.getByRole("dialog", { name: tx("confirm.voidTitle") });
    await expect(dialog).toContainText(tx("confirm.voidBody"));
    await expect(dialog.getByRole("button", { name: tx("confirm.cancel"), exact: true })).toBeFocused();
    await dialog.getByRole("button", { name: tx("confirm.cancel"), exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(tx("invoiceStatus.draft"), { exact: true })).toBeVisible();
    await page.getByRole("button", { name: tx("billing.void"), exact: true }).click();
    await dialog.getByRole("button", { name: tx("confirm.void"), exact: true }).click();
    await expect(page.getByText(tx("invoiceStatus.void"), { exact: true })).toBeVisible();
  });

  // POST /api/v1/consents/{consent_id}/withdraw
  test("the patient withdraws consent", async ({ page }) => {
    await page.goto("/patient/login");
    await page.getByLabel(tx("auth.email")).fill(patientEmail);
    const since = Date.now() - 1000;
    await page.getByRole("button", { name: tx("portal.sendCode"), exact: true }).click();
    let code: string | null = null;
    await expect.poll(async () => (code = await emailCode(patientEmail, since)), { timeout: process.env.E2E_NATIVE ? 60_000 : 15_000 }).not.toBeNull();
    await page.getByLabel(tx("auth.code")).fill(code ?? "");
    await page.getByRole("button", { name: tx("auth.signIn"), exact: true }).click();
    await page.getByRole("tab", { name: tx("portal.tabs.consent"), exact: true }).click();
    page.once("dialog", (dialog) => void dialog.accept());
    const withdrawn = page.waitForResponse((r) => /\/api\/v1\/consents\/[^/]+\/withdraw$/.test(r.url()));
    await page.getByRole("button", { name: tx("portal.withdraw"), exact: true }).first().click();
    expect((await withdrawn).status()).toBe(200);
  });

  // POST /api/v1/consent-notices, POST /api/v1/consent-notices/{notice_id}/publish,
  // POST /api/v1/schedules, POST /api/v1/services, POST /api/v1/shifts,
  // POST /api/v1/shifts/{shift_id}/cancel
  test("admin publishes a notice and adds a schedule, a service and a shift", async ({ page }) => {
    await signIn(page, "clinic_admin");
    await page.getByRole("tab", { name: tx("admin.tabs.consent"), exact: true }).click();
    // The new version keeps the current text, so the registration flow still finds it, and says
    // plainly that it is a test version.
    const current = page.getByRole("article").filter({ hasText: tx("admin.publishedOn", { date: "" }).trim() }).first();
    const [en = "", hi = ""] = (await current.locator("p.line-clamp-3").allInnerTexts()).map((t) => t.replace(` ${TEST_MARK}`, ""));
    await page.locator("#n-en").fill(`${en} ${TEST_MARK}`);
    await page.locator("#n-hi").fill(`${hi} ${TEST_MARK}`);
    await page.getByRole("button", { name: tx("admin.saveDraft"), exact: true }).click();
    const draft = page.getByRole("article").filter({ hasText: TEST_MARK }).filter({ has: page.getByRole("button", { name: tx("admin.publish"), exact: true }) }).first();
    await draft.getByRole("button", { name: tx("admin.publish"), exact: true }).click();
    await expect(page.getByRole("article").filter({ hasText: TEST_MARK }).first()).toContainText(tx("admin.publishedOn", { date: "" }).trim());

    await page.getByRole("tab", { name: tx("admin.tabs.schedules"), exact: true }).click();
    await page.locator("#sch-doctor").selectOption({ label: doctor2.name });
    await page.locator("#sch-day").selectOption("6");
    await page.locator("#sch-start").fill("10:00");
    await page.locator("#sch-end").fill("12:00");
    const schedule = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/api/v1/schedules"));
    await page.getByRole("button", { name: tx("admin.add"), exact: true }).click();
    expect((await schedule).status()).toBe(201);

    await page.getByRole("tab", { name: tx("admin.tabs.services"), exact: true }).click();
    await page.locator("#svc-name").fill(`Spirometry ${stamp}`);
    await page.locator("#svc-price").fill("450");
    await page.getByRole("button", { name: tx("admin.add"), exact: true }).click();
    await expect(page.getByRole("listitem").filter({ hasText: `Spirometry ${stamp}` })).toBeVisible();

    await page.getByRole("tab", { name: tx("admin.tabs.shifts"), exact: true }).click();
    await page.locator("#shift-member").selectOption({ label: `${doctor2.name} · ${tx("roles.doctor")}` });
    const day = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10);
    await page.locator("#shift-day").fill(day);
    await page.locator("#shift-start").fill("09:00");
    await page.locator("#shift-end").fill("13:00");
    await page.getByRole("button", { name: tx("admin.add"), exact: true }).click();
    await expect(page.getByText(tx("admin.shiftAdded"))).toBeVisible();
    const shift = page.getByRole("listitem").filter({ hasText: doctor2.name }).first();
    const cancelled = page.waitForResponse((r) => /\/api\/v1\/shifts\/[^/]+\/cancel$/.test(r.url()));
    await shift.getByRole("button", { name: tx("admin.cancelShift"), exact: true }).click();
    expect((await cancelled).status()).toBe(200);
  });

  // DELETE /api/v1/auth/sessions/{session_id}
  test("staff sign out another of their sessions", async ({ page, browser }) => {
    const other = await browser.newContext();
    await signIn(await other.newPage(), "lab_tech");
    await signIn(page, "lab_tech");
    await page.goto("/account/sessions");
    const signedOut = page.waitForResponse((r) => r.request().method() === "DELETE" && /\/api\/v1\/auth\/sessions\//.test(r.url()));
    await page.getByRole("button", { name: tx("sessions.revoke"), exact: true }).first().click();
    expect((await signedOut).status()).toBe(204);
    await expect(page.getByTestId("session-confirmation")).toContainText(tx("sessions.revoked"));
    await other.close();
  });
});

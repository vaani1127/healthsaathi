import { expect, test } from "@playwright/test";

import { emailCode, LANG, OTHER_LANG, signIn, tx, txIn, user } from "./support";

/** "Dr X (Doctor) opened your notes because of appointment token", in the language of the run:
 * the same templates the patient's access log uses. */
function appointmentSentenceStart(doctor: string): string {
  const did = tx("actions.view", { who: `${doctor} (${tx("roles.doctor")})`, what: tx("resources.notes") });
  const mark = "";
  return tx("why.appointmentToken", { did, token: mark, time: mark }).split(mark)[0] ?? did;
}

// SPEC 1.3 flows 1 to 6, run in order on one patient.
test.describe.serial("OPD day", () => {
  const stamp = Date.now().toString().slice(-6);
  const patientName = `Kavita Demo ${stamp}`;
  const patientEmail = `kavita.${stamp}@e2e.healthsaathi.test`;

  test("flow 1: reception registers a patient with consent, books and issues a token", async ({ page }) => {
    await signIn(page, "reception");
    await expect(page).toHaveURL(/\/reception$/);

    await page.getByRole("button", { name: tx("reception.registerNew") }).click();
    await page.getByLabel(tx("patient.name")).fill(patientName);
    await page.getByLabel(tx("patient.sex")).selectOption("female");
    await page.getByLabel(tx("patient.phone")).fill("+91-00000-12345");
    await expect(page.getByTestId("consent-text")).toContainText(LANG === "hi" ? "यह क्लिनिक आपके इलाज" : "This clinic records your health information");

    const register = page.getByRole("button", { name: tx("reception.register"), exact: true });
    await expect(register).toBeDisabled();
    await page.getByLabel(tx("consent.agreed")).check();
    await register.click();

    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();
    await expect(page.getByText(tx("consent.given", { version: 1 }))).toBeVisible();

    await page.getByLabel(tx("reception.doctor")).first().selectOption({ label: user("doctor").name });
    // A few minutes ahead, so the appointment explains the visit. A stack that already has
    // bookings (the demo) may have that slot taken: then try a few minutes later.
    const pad = (n: number) => String(n).padStart(2, "0");
    const booked = page.getByText(tx("reception.booked"));
    for (let attempt = 0; attempt < 8; attempt++) {
      const at = new Date(Date.now() + (2 + attempt * 6) * 60_000);
      await page
        .locator("#book-when")
        .fill(`${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`);
      await page.getByRole("button", { name: tx("reception.book") }).click();
      await page.waitForLoadState("networkidle");
      await expect(booked.or(page.getByText(tx("errors.slot-taken")))).toBeVisible();
      if (await booked.isVisible()) {
        break;
      }
    }
    await expect(booked).toBeVisible();

    await page.getByRole("link", { name: tx("app.name") }).click();
    const row = page.getByRole("listitem").filter({ hasText: patientName });
    await row.getByRole("button", { name: tx("reception.issueToken") }).click();
    await expect(row.getByText(new RegExp(tx("reception.tokenNo", { no: "XX" }).replace("XX", "\\d+")))).toBeVisible();
  });

  test("flow 2: nurse opens today's queue and records vitals", async ({ page }) => {
    await signIn(page, "nurse");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();

    await page.getByRole("button", { name: tx("queue.takeIn") }).click();
    await page.getByLabel(tx("vitals.bp_sys")).fill("124");
    await page.getByLabel(tx("vitals.bp_dia")).fill("82");
    await page.getByLabel(tx("vitals.pulse")).fill("76");
    await page.getByLabel(tx("vitals.temp_c")).fill("37.4");
    await page.getByLabel(tx("vitals.spo2")).fill("98");
    await page.getByRole("button", { name: tx("vitals.save") }).click();
    await expect(page.getByText(tx("vitals.saved"))).toBeVisible();
    await expect(page.getByTestId("vitals-list")).toContainText(`${tx("vitals.bp")} 124/82`);
    await page.getByRole("button", { name: tx("queue.sendToDoctor") }).click();
  });

  test("flow 3: doctor writes and signs a note, prescribes, orders a lab test and books follow-up", async ({ page }) => {
    await signIn(page, "doctor");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();
    await expect(page.getByText(tx("templates.T_APPT"), { exact: true })).toBeVisible();

    await page.getByRole("button", { name: tx("consult.start") }).click();

    await page.getByLabel(tx("notes.body"), { exact: true }).fill("Fever for 2 days. Throat red. No rash.");
    await page.getByRole("button", { name: tx("notes.saveDraft") }).click();
    await page.getByRole("button", { name: tx("notes.sign") }).click();
    await expect(page.getByText(tx("notes.signed"))).toBeVisible();

    await page.getByLabel(tx("rx.drug"), { exact: true }).fill("Paracetamol");
    await page.getByLabel(tx("rx.strength")).fill("500 mg");
    await page.getByLabel(tx("rx.dose")).fill("1-1-1");
    await page.getByLabel(tx("rx.days")).fill("3");
    await page.getByLabel(tx("rx.instructionsHi")).fill("खाने के बाद लें");
    await page.getByLabel(tx("rx.adviceHi")).fill("आराम करें और पानी पिएँ");
    await page.getByRole("button", { name: tx("rx.save") }).click();
    await expect(page.getByText("Prescription saved.")).toBeVisible();

    await page.getByRole("checkbox", { name: tx("lab.tests.CBC") }).check();
    await page.getByRole("button", { name: tx("lab.placeOrder") }).click();
    await expect(page.getByText(tx("lab.ordered"))).toBeVisible();

    await page.getByRole("button", { name: tx("followUp.book") }).click();
    await expect(page.getByText(tx("followUp.booked"))).toBeVisible();

    await page.getByRole("button", { name: tx("rx.sign"), exact: true }).click();
    await page.getByRole("link", { name: tx("rx.print") }).click();
    await expect(page.getByText("Paracetamol")).toBeVisible();
    await expect(page.getByText("खाने के बाद लें")).toBeVisible();
    await expect(page.getByText("Medicine / दवा")).toBeVisible();
    await expect(page.getByRole("button", { name: tx("rx.printNow") })).toBeVisible();
  });

  test("flow 4: lab marks the sample collected and uploads the result; the doctor sees it", async ({ page }) => {
    await signIn(page, "lab_tech");
    const item = page.getByTestId("work-item").filter({ hasText: patientName });
    await item.getByRole("button", { name: tx("lab.collected") }).click();
    await item.getByLabel(tx("lab.valueFor", { test: tx("lab.tests.CBC") })).fill("13.4");
    await item.getByLabel(tx("lab.unit")).fill("g/dL");
    await item.getByLabel(tx("lab.report")).setInputFiles({
      name: "cbc.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 synthetic CBC report"),
    });
    await item.getByRole("button", { name: tx("lab.saveAndRelease") }).click();
    await expect(page.getByText(tx("lab.released"))).toBeVisible();

    await page.getByRole("button", { name: tx("auth.signOut") }).click();
    await signIn(page, "doctor");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByTestId("result-values")).toContainText("13.4 g/dL");
  });

  test("flow 5: reception bills the visit, records payment and prints the receipt", async ({ page }) => {
    await signIn(page, "reception");
    await page.getByLabel(tx("reception.searchLabel")).fill(patientName);
    await page.getByRole("link", { name: new RegExp(patientName) }).click();

    await page.getByLabel(/^Consultation/).fill("1");
    await page.getByRole("button", { name: tx("billing.create") }).click();
    await page.getByRole("button", { name: tx("billing.issue") }).click();
    await page.getByRole("button", { name: tx("billing.markPaid") }).click();
    await expect(page.getByText(tx("invoiceStatus.paid"), { exact: true })).toBeVisible();
    await page.getByRole("link", { name: tx("billing.printReceipt") }).click();
    await expect(page.getByText("Receipt / रसीद")).toBeVisible();
    await expect(page.getByText(/Total \/ कुल/)).toContainText("300");

    await page.goBack();
    await page.getByRole("link", { name: tx("billing.backToPatient") }).click();
    await page.getByLabel(tx("auth.email"), { exact: true }).fill(patientEmail);
    await page.getByRole("button", { name: tx("reception.portalGrant") }).click();
    await expect(page.getByText(tx("reception.portalSent"))).toBeVisible();
  });

  test("flow 6: the patient signs in by email code, sees records and the access log, and reports an access", async ({ page }) => {
    await page.goto("/patient/login");
    await page.getByLabel(tx("auth.email")).fill(patientEmail);
    const sentAfter = Date.now() - 1000;
    await page.getByRole("button", { name: tx("portal.sendCode") }).click();
    await expect(page.getByLabel(tx("auth.code"))).toBeVisible();
    let code: string | null = null;
    await expect
      .poll(async () => (code = await emailCode(patientEmail, sentAfter)), { timeout: process.env.E2E_NATIVE ? 60_000 : 10_000 })
      .not.toBeNull();
    await page.getByLabel(tx("auth.code")).fill(code ?? "");
    await page.getByRole("button", { name: tx("auth.signIn") }).click();

    await expect(page.getByRole("heading", { name: tx("portal.hello", { name: patientName }) })).toBeVisible();
    await expect(page.getByText("Paracetamol")).toBeVisible();
    await expect(page.getByTestId("result-values")).toContainText("13.4 g/dL");

    await page.getByRole("tab", { name: tx("portal.tabs.log") }).click();
    const doctorEntry = page
      .getByTestId("access-entry")
      .filter({ hasText: appointmentSentenceStart(user("doctor").name) })
      .first();
    await expect(doctorEntry).toBeVisible();
    await expect(doctorEntry).toContainText(/\d{1,2}:\d{2}/);

    const entry = page.getByTestId("access-entry").first();
    await entry.getByRole("button", { name: tx("portal.notMe") }).click();
    await entry.getByLabel(tx("portal.notMeMessage")).fill("This is a test report.");
    await entry.getByRole("button", { name: tx("portal.sendReport") }).click();
    await expect(entry.getByText(tx("portal.queryStatus.open"))).toBeVisible();

    await page.getByRole("button", { name: tx("language.switch") }).click();
    await expect(page.getByRole("tab", { name: txIn(OTHER_LANG, "portal.tabs.log") })).toBeVisible();
    await expect(page.getByText(txIn(OTHER_LANG, "portal.queryStatus.open")).first()).toBeVisible();
    await page.getByRole("button", { name: txIn(OTHER_LANG, "language.switch") }).click();
  });
});

test("staff screens fit a 360 px wide phone", async ({ page }) => {
  await signIn(page, "reception");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

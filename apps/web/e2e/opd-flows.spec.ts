import { expect, test } from "@playwright/test";

import { emailCode, signIn, user } from "./support";

// SPEC 1.3 flows 1 to 6, run in order on one patient.
test.describe.serial("OPD day", () => {
  const stamp = Date.now().toString().slice(-6);
  const patientName = `Kavita Demo ${stamp}`;
  const patientEmail = `kavita.${stamp}@e2e.healthsaathi.test`;

  test("flow 1: reception registers a patient with consent, books and issues a token", async ({ page }) => {
    await signIn(page, "reception");
    await expect(page).toHaveURL(/\/reception$/);

    await page.getByRole("button", { name: "Register new patient" }).click();
    await page.getByLabel("Full name").fill(patientName);
    await page.getByLabel("Sex").selectOption("female");
    await page.getByLabel("Phone").fill("+91-00000-12345");
    await expect(page.getByTestId("consent-text")).toContainText("This clinic records your health information");

    const register = page.getByRole("button", { name: "Register", exact: true });
    await expect(register).toBeDisabled();
    await page.getByLabel("I read this notice to the patient and the patient agreed.").check();
    await register.click();

    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();
    await expect(page.getByText("Given (notice v1)")).toBeVisible();

    await page.getByLabel("Doctor").first().selectOption({ label: user("doctor").name });
    await page.getByRole("button", { name: "Book appointment" }).click();
    await expect(page.getByText("Appointment booked.")).toBeVisible();

    await page.getByRole("link", { name: "HealthSaathi" }).click();
    const row = page.getByRole("listitem").filter({ hasText: patientName });
    await row.getByRole("button", { name: "Issue token" }).click();
    await expect(row.getByText(/Token \d+/)).toBeVisible();
  });

  test("flow 2: nurse opens today's queue and records vitals", async ({ page }) => {
    await signIn(page, "nurse");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();

    await page.getByRole("button", { name: "Take in" }).click();
    await page.getByLabel("BP systolic").fill("124");
    await page.getByLabel("BP diastolic").fill("82");
    await page.getByLabel("Pulse").fill("76");
    await page.getByLabel("Temp °C").fill("37.4");
    await page.getByLabel("SpO2 %").fill("98");
    await page.getByRole("button", { name: "Save vitals" }).click();
    await expect(page.getByText("Vitals saved.")).toBeVisible();
    await expect(page.getByTestId("vitals-list")).toContainText("BP 124/82");
    await page.getByRole("button", { name: "Send to doctor" }).click();
  });

  test("flow 3: doctor writes and signs a note, prescribes, orders a lab test and books follow-up", async ({ page }) => {
    await signIn(page, "doctor");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByRole("heading", { name: patientName })).toBeVisible();
    await expect(page.getByText("Appointment", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Start consultation" }).click();

    await page.getByLabel("Note", { exact: true }).fill("Fever for 2 days. Throat red. No rash.");
    await page.getByRole("button", { name: "Save draft" }).click();
    await page.getByRole("button", { name: "Sign note" }).click();
    await expect(page.getByText("Note signed. It cannot be changed now.")).toBeVisible();

    await page.getByLabel("Medicine", { exact: true }).fill("Paracetamol");
    await page.getByLabel("Strength").fill("500 mg");
    await page.getByLabel("Dose").fill("1-1-1");
    await page.getByLabel("Days").fill("3");
    await page.getByLabel("Instructions (Hindi)").fill("खाने के बाद लें");
    await page.getByLabel("Advice (Hindi)").fill("आराम करें और पानी पिएँ");
    await page.getByRole("button", { name: "Save prescription" }).click();
    await expect(page.getByText("Prescription saved.")).toBeVisible();

    await page.getByRole("checkbox", { name: "Complete blood count" }).check();
    await page.getByRole("button", { name: "Place order" }).click();
    await expect(page.getByText("Lab tests ordered.")).toBeVisible();

    await page.getByRole("button", { name: "Book follow-up" }).click();
    await expect(page.getByText("Follow-up booked.")).toBeVisible();

    await page.getByRole("button", { name: "Sign", exact: true }).click();
    await page.getByRole("link", { name: "Print" }).click();
    await expect(page.getByText("Paracetamol")).toBeVisible();
    await expect(page.getByText("खाने के बाद लें")).toBeVisible();
    await expect(page.getByText("Medicine / दवा")).toBeVisible();
    await expect(page.getByRole("button", { name: "Print or save as PDF" })).toBeVisible();
  });

  test("flow 4: lab marks the sample collected and uploads the result; the doctor sees it", async ({ page }) => {
    await signIn(page, "lab_tech");
    const item = page.getByTestId("work-item").filter({ hasText: patientName });
    await item.getByRole("button", { name: "Sample collected" }).click();
    await item.getByLabel("Complete blood count result").fill("13.4");
    await item.getByLabel("Unit").fill("g/dL");
    await item.getByLabel("Report file").setInputFiles({
      name: "cbc.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 synthetic CBC report"),
    });
    await item.getByRole("button", { name: "Save and release result" }).click();
    await expect(page.getByText("Result saved and released to the doctor.")).toBeVisible();

    await page.getByRole("button", { name: "Sign out" }).click();
    await signIn(page, "doctor");
    await page.getByRole("link", { name: new RegExp(patientName) }).click();
    await expect(page.getByTestId("result-values")).toContainText("13.4 g/dL");
  });

  test("flow 5: reception bills the visit, records payment and prints the receipt", async ({ page }) => {
    await signIn(page, "reception");
    await page.getByLabel("Search patients").fill(patientName);
    await page.getByRole("link", { name: new RegExp(patientName) }).click();

    await page.getByLabel(/^Consultation/).fill("1");
    await page.getByRole("button", { name: "Create invoice" }).click();
    await page.getByRole("button", { name: "Issue invoice" }).click();
    await page.getByRole("button", { name: "Record payment" }).click();
    await expect(page.getByText("Paid", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Print receipt" }).click();
    await expect(page.getByText("Receipt / रसीद")).toBeVisible();
    await expect(page.getByText(/Total \/ कुल/)).toContainText("300");

    await page.goBack();
    await page.getByRole("link", { name: "Back to patient" }).click();
    await page.getByLabel("Email", { exact: true }).fill(patientEmail);
    await page.getByRole("button", { name: "Give portal access" }).click();
    await expect(page.getByText("Done. The patient can now sign in with this email.")).toBeVisible();
  });

  test("flow 6: the patient signs in by email code, sees records and the access log, and reports an access", async ({ page }) => {
    await page.goto("/patient/login");
    await page.getByLabel("Email").fill(patientEmail);
    const sentAfter = Date.now() - 1000;
    await page.getByRole("button", { name: "Send code" }).click();
    await expect(page.getByLabel("6 digit code")).toBeVisible();
    let code: string | null = null;
    await expect
      .poll(() => (code = emailCode(patientEmail, sentAfter)), { timeout: 10_000 })
      .not.toBeNull();
    await page.getByLabel("6 digit code").fill(code ?? "");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("heading", { name: `Hello, ${patientName}` })).toBeVisible();
    await expect(page.getByText("Paracetamol")).toBeVisible();
    await expect(page.getByTestId("result-values")).toContainText("13.4 g/dL");

    await page.getByRole("tab", { name: "Who opened my record" }).click();
    const doctorEntry = page
      .getByTestId("access-entry")
      .filter({ hasText: `${user("doctor").name} (Doctor) opened your notes because of appointment token` })
      .first();
    await expect(doctorEntry).toBeVisible();
    await expect(doctorEntry).toContainText(/at \d{1,2}:\d{2}/);

    const entry = page.getByTestId("access-entry").first();
    await entry.getByRole("button", { name: "I do not recognise this" }).click();
    await entry.getByLabel("Tell us what looks wrong").fill("This is a test report.");
    await entry.getByRole("button", { name: "Send to the clinic" }).click();
    await expect(entry.getByText("Reported to the clinic")).toBeVisible();

    await page.getByRole("button", { name: "हिन्दी" }).click();
    await expect(page.getByRole("tab", { name: "मेरा रिकॉर्ड किसने खोला" })).toBeVisible();
    await expect(page.getByText("क्लिनिक को बताया गया").first()).toBeVisible();
    await page.getByRole("button", { name: "English" }).click();
  });
});

test("staff screens fit a 360 px wide phone", async ({ page }) => {
  await signIn(page, "reception");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

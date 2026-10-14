/**
 * Screenshots of every screen of the running demo, for reviews without the app.
 *
 *     make demo-screenshots        (the demo must be running: make demo-native or make demo)
 *
 * Signs in as each role the same way as the browser tests (staff codes from the demo helper,
 * patient codes from the dev mailbox), waits for every screen to finish loading, and saves
 * full-page PNGs at desktop (1440x900) and phone (360x800) size into demo-screenshots/, named
 * <nn>-<role>-<screen>-<desktop|mobile>[-hi].png in the order of the demo flows. The main screen
 * of each role is also taken in Hindi. Writes demo-screenshots/index.html (all shots with a
 * caption, grouped by role) and demo-screenshots/screens.pdf.
 *
 * Never shows a filled password field or an authenticator key: the login page is taken empty,
 * the code page before the code is typed, and the authenticator setup page is not taken.
 * The tamper shot edits one audit row as the database owner and always puts it back.
 *
 * Runs with Node 24 (TypeScript types are stripped); Playwright comes from apps/web.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const WEB = join(ROOT, "apps", "web");
const OUT = join(ROOT, "demo-screenshots");
const BASE = process.env.DEMO_URL ?? "http://localhost:8080";
const NATIVE = !process.env.DEMO_DOCKER;
const { chromium } = createRequire(join(WEB, "package.json"))("@playwright/test") as typeof import("@playwright/test");

const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 360, height: 800 };
const STATE = JSON.parse(readFileSync(join(ROOT, ".demo", "state", "logins.json"), "utf-8")) as { password: string };
const CLINIC = "Saathi Demo Clinic A";

interface Shot {
  role: string;
  caption: string;
  files: string[];
}
const shots: Shot[] = [];
const failures: string[] = [];
let counter = 0;

function helper(args: string[]): string {
  const cmd = NATIVE ? ["run", "python", "-m", "hs_demo.native", ...args] : [];
  if (!NATIVE) {
    throw new Error("only native mode is supported; use make demo-native");
  }
  return execFileSync("uv", cmd, { cwd: ROOT, encoding: "utf-8" }).trim();
}

function sql(statement: string): string {
  return helper(["sql", statement]);
}

/** Waits until no request is running: network idle, no top progress bar, no loading text. */
async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 180_000 }).catch(() => undefined);
  const idle = () =>
    page.waitForFunction(
      () =>
        !document.querySelector("[role=progressbar][aria-busy=true]") &&
        ![...document.querySelectorAll("[role=status]")].some((e) => /^(Loading|लोड हो रहा है)/.test((e.textContent ?? "").trim())),
      null,
      { timeout: 180_000, polling: 250 },
    );
  await idle();
  await page.waitForTimeout(700); // the bar appears only after 300 ms; check it stayed away
  await idle();
  await page.waitForLoadState("networkidle", { timeout: 60_000 }).catch(() => undefined);
}

async function language(page: Page, lang: "en" | "hi"): Promise<void> {
  await page.evaluate((l) => localStorage.setItem("hs.lang", l), lang);
  await page.reload();
  await settle(page);
}

/** One screen: desktop and phone PNGs (and Hindi ones when `hindi`). */
async function shot(page: Page, role: string, screen: string, caption: string, hindi = false): Promise<void> {
  counter += 1;
  const nn = String(counter).padStart(2, "0");
  const files: string[] = [];
  try {
    for (const lang of hindi ? (["en", "hi"] as const) : (["en"] as const)) {
      if (lang === "hi") {
        await language(page, "hi");
      }
      for (const [size, viewport] of [["desktop", DESKTOP], ["mobile", MOBILE]] as const) {
        await page.setViewportSize(viewport);
        await settle(page);
        const file = `${nn}-${role}-${screen}-${size}${lang === "hi" ? "-hi" : ""}.png`;
        await page.screenshot({ path: join(OUT, file), fullPage: true });
        files.push(file);
      }
      if (lang === "hi") {
        await language(page, "en");
      }
    }
  } catch (err) {
    failures.push(`${nn}-${role}-${screen}: ${(err as Error).message.split("\n")[0]}`);
  }
  await page.setViewportSize(DESKTOP);
  shots.push({ role, caption, files });
  console.log(`${nn} ${role} ${screen}: ${files.length} files`);
}

/** Runs a step that leads to a screen; a failure is recorded and the walk goes on. */
async function step(name: string, run: () => Promise<void>): Promise<void> {
  try {
    await run();
  } catch (err) {
    failures.push(`${name}: ${(err as Error).message.split("\n")[0]}`);
    console.log(`FAILED ${name}`);
  }
}

async function staffSignIn(
  page: Page,
  email: string,
  before?: () => Promise<void>,
  atCode?: () => Promise<void>,
): Promise<void> {
  await page.goto(`${BASE}/login`);
  await settle(page);
  if (before) {
    await before(); // the empty login page
  }
  await page.locator("#email").fill(email);
  await page.locator("#password").fill(STATE.password);
  await page.locator("form button[type=submit]").click();
  await page.locator("#code").waitFor({ timeout: 120_000 });
  await page.locator("#password").waitFor({ state: "detached" }).catch(() => undefined);
  if (atCode) {
    await settle(page);
    await atCode(); // the code page, before any code is typed
  }
  for (let attempt = 0; attempt < 4; attempt++) {
    await page.locator("#code").fill(helper(["code", email]));
    await page.locator("form button[type=submit]").click();
    try {
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 90_000 });
      await settle(page);
      return;
    } catch {
      await page.waitForTimeout(31_000); // this code was used already: wait for the next one
    }
  }
  throw new Error(`could not sign in as ${email}`);
}

async function patientSignIn(page: Page, email: string): Promise<void> {
  await page.goto(`${BASE}/patient/login`);
  await settle(page);
  const since = Date.now() - 5_000;
  await page.locator("#p-email").fill(email);
  await page.locator("form button[type=submit]").click();
  let code: string | null = null;
  for (let i = 0; i < 60 && !code; i++) {
    const mails = (await (await fetch(`${BASE}/api/v1/dev/mailbox?to=${encodeURIComponent(email)}`)).json()) as {
      text: string;
      sent_at: string | null;
    }[];
    for (const mail of mails) {
      const match = /\b(\d{6})\b/.exec(mail.text);
      if (match?.[1] && mail.sent_at && Date.parse(mail.sent_at) >= since) {
        code = match[1];
        break;
      }
    }
    if (!code) {
      await page.waitForTimeout(2_000);
    }
  }
  if (!code) {
    throw new Error(`no sign-in code for ${email}`);
  }
  await page.locator("#p-code").fill(code);
  await page.locator("form button[type=submit]").click();
  await page.waitForURL((url) => url.pathname.startsWith("/patient") && !url.pathname.startsWith("/patient/login"), {
    timeout: 120_000,
  });
  await settle(page);
}

/** A Clinic A patient with vitals, notes, a prescription, a released lab result and a bill. */
function richPatient(): { id: string; name: string; rx: string; lab: string; invoice: string } {
  const row = sql(
    "SELECT concat_ws('|', p.id, p.name," +
      " (SELECT max(r.id::text) FROM prescriptions r WHERE r.patient_id = p.id)," +
      " (SELECT max(o.id::text) FROM lab_orders o JOIN lab_results lr ON lr.lab_order_id = o.id" +
      "   WHERE o.patient_id = p.id AND lr.released_at IS NOT NULL)," +
      " (SELECT max(i.id::text) FROM invoices i WHERE i.patient_id = p.id))" +
      " FROM patients p JOIN clinics c ON c.id = p.clinic_id WHERE c.name = '" + CLINIC + "'" +
      " ORDER BY (SELECT count(*) FROM lab_results lr WHERE lr.patient_id = p.id AND lr.released_at IS NOT NULL) DESC," +
      " (SELECT count(*) FROM prescriptions r WHERE r.patient_id = p.id) DESC LIMIT 1",
  );
  const [id = "", name = "", rx = "", lab = "", invoice = ""] = row.split("|");
  return { id, name, rx, lab, invoice };
}

const TAMPER_ROW = `(SELECT min(a.seq) FROM audit_events a JOIN clinics c ON c.id = a.clinic_id WHERE c.name = '${CLINIC}')`;
const TAMPER =
  "BEGIN; ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only; " +
  `UPDATE audit_events SET payload = payload || '{"tampered": true}' WHERE seq = ${TAMPER_ROW}; ` +
  "ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only; COMMIT;";
const UNDO =
  "BEGIN; ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only; " +
  `UPDATE audit_events SET payload = payload - 'tampered' WHERE seq = ${TAMPER_ROW}; ` +
  "ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only; COMMIT;";

async function main(): Promise<void> {
  mkdirSync(OUT, { recursive: true });
  const rich = richPatient();
  const browser = await chromium.launch();
  const fresh = async (): Promise<Page> => {
    const context = await browser.newContext({ viewport: DESKTOP, locale: "en-IN", timezoneId: "Asia/Kolkata" });
    await context.addInitScript(() => {
      if (!localStorage.getItem("hs.lang")) {
        localStorage.setItem("hs.lang", "en");
      }
    });
    return context.newPage();
  };

  // Public screens ------------------------------------------------------------------------------
  let page = await fresh();
  await step("public", async () => {
    await page.goto(`${BASE}/patient/login`);
    await settle(page);
    await shot(page, "public", "patient-login", "Patients sign in with a code sent to their email; no password.");
    await page.goto(`${BASE}/verify`);
    await settle(page);
    await shot(page, "public", "verify-receipt", "Anyone can paste an access receipt here and check it in the browser.");
    await page.goto(`${BASE}/login`);
    await settle(page);
    await page.locator("#email").fill("reception1.a@demo.healthsaathi.test");
    await page.locator("#password").fill("not-the-password");
    await page.locator("form button[type=submit]").click();
    await page.getByRole("alert").first().waitFor({ timeout: 120_000 });
    await page.locator("#password").fill("");
    await shot(page, "public", "login-error", "Validation: a wrong password shows a plain error and the field is cleared.");
  });
  await page.context().close();

  // Reception ----------------------------------------------------------------------------------
  page = await fresh();
  await step("reception", async () => {
    await staffSignIn(page, "reception1.a@demo.healthsaathi.test", async () => {
      await shot(page, "public", "staff-login", "Staff sign in with email and password, then a code from an authenticator app.", true);
    }, async () => {
      await shot(page, "public", "staff-2fa", "Second step: the 6-digit code from the staff member's authenticator app.");
    });
    await shot(page, "reception", "home", "Reception home: today's appointments, search and register patients, issue queue tokens.", true);
    await page.getByRole("button", { name: /Register new patient/ }).click();
    await settle(page);
    await shot(page, "reception", "register-form", "Registration form with the consent notice the patient agrees to.");
    await page.goto(`${BASE}/reception`);
    await settle(page);
    const search = page.getByLabel(/Search/).first();
    await search.fill("Nobody Zzzz");
    await settle(page);
    await shot(page, "reception", "search-empty", "Empty state: a search with no matching patient.");
    await page.goto(`${BASE}/reception/patients/${rich.id}`);
    await settle(page);
    await shot(page, "reception", "patient-booking", "A patient's page at reception, with the booking form for a new appointment.");
    await page.goto(`${BASE}/reception/invoices/${rich.invoice}`);
    await settle(page);
    await shot(page, "reception", "invoice", "A bill with its items and payments.");
    await page.goto(`${BASE}/print/receipt/${rich.invoice}`);
    await settle(page);
    await shot(page, "reception", "receipt-print", "Printable receipt in English and Hindi.");
  });
  await page.context().close();

  // Nurse --------------------------------------------------------------------------------------
  page = await fresh();
  await step("nurse", async () => {
    await staffSignIn(page, "nurse1.a@demo.healthsaathi.test");
    await shot(page, "nurse", "queue", "Nurse home: today's queue with token numbers.", true);
    const first = page.locator("a[href^='/nurse/patients/']").first();
    if (await first.count()) {
      await first.click();
      await settle(page);
      await shot(page, "nurse", "patient-vitals", "A patient from the queue: vitals form and earlier vitals.");
      await page.context().setOffline(true);
      await page.waitForTimeout(1_500);
      await shot(page, "nurse", "vitals-offline", "Offline: vitals are kept on the phone and sent when the connection is back.");
      await page.context().setOffline(false);
    } else {
      failures.push("nurse patient-vitals: today's queue is empty");
    }
  });
  await page.context().close();

  // Doctor -------------------------------------------------------------------------------------
  page = await fresh();
  await step("doctor", async () => {
    await staffSignIn(page, "doctor1.a@demo.healthsaathi.test");
    await shot(page, "doctor", "queue", "Doctor home: my queue for today.", true);
    await page.goto(`${BASE}/doctor/patients/${rich.id}`);
    await settle(page);
    if (await page.locator("#reason-code").count()) {
      await shot(page, "doctor", "reason-needed", "No appointment explains this access: the doctor must type a reason or use break-glass.");
      await page.getByRole("button", { name: /Break glass/i }).click();
      await settle(page);
      await shot(page, "doctor", "break-glass-dialog", "Break-glass: choose an emergency reason; it opens a 4-hour emergency view and is reviewed.");
      await page.goto(`${BASE}/doctor/patients/${rich.id}`);
      await settle(page);
      await page.locator("#reason-code").selectOption("second_opinion");
      await page.locator("#reason-text").fill("Reviewing the lab trend for the mentor demo");
      await page.locator("#reason-text").press("Tab");
      await shot(page, "doctor", "typed-reason", "Typed reason filled in; it is saved with the access and shown to the admin.");
      await page.getByRole("button", { name: /Open record/i }).click();
      await settle(page);
    }
    await shot(page, "doctor", "chart", "Patient chart: vitals, allergies, notes, prescriptions, lab results, with the access explanation.");
    if (rich.rx) {
      await page.goto(`${BASE}/print/prescription/${rich.rx}`);
      await settle(page);
      await shot(page, "doctor", "prescription-print", "Printable prescription in English and Hindi.");
    }
    if (rich.lab) {
      await page.goto(`${BASE}/print/lab/${rich.lab}`);
      await settle(page);
      await shot(page, "doctor", "lab-report-print", "Printable lab report.");
    }
    await page.goto(`${BASE}/account/sessions`);
    await settle(page);
    await shot(page, "doctor", "sessions", "Account: signed-in devices with sign-out per device, and password change.");
  });
  await page.context().close();

  // Lab technician -----------------------------------------------------------------------------
  page = await fresh();
  await step("lab", async () => {
    await staffSignIn(page, "lab.a@demo.healthsaathi.test");
    await shot(page, "lab", "worklist", "Lab worklist: mark samples collected, enter results, upload the report and release it.", true);
  });
  await page.context().close();

  // Clinic admin -------------------------------------------------------------------------------
  page = await fresh();
  await step("admin", async () => {
    await staffSignIn(page, "admin.a@demo.healthsaathi.test");
    await shot(page, "admin", "alerts", "Admin home: today's ranked alerts within the daily alert budget.", true);
    const alert = page.getByTestId("alert").first();
    if (await alert.count()) {
      await alert.getByRole("button", { name: /Open/ }).click();
      await settle(page);
      await shot(page, "admin", "alert-detail", "One alert: why it was flagged, top features, the user's recent accesses, and review buttons.");
    } else {
      failures.push("admin alert-detail: no open alert");
    }
    await page.getByRole("button", { name: "One day" }).click();
    const day = page.locator("#alerts-day");
    if (await day.count()) {
      await day.fill("2026-01-01");
      await settle(page);
      await shot(page, "admin", "alerts-empty-day", "Empty state: a day with no alerts.");
    }
    const tabs: [string, string, string][] = [
      ["staff", "Staff", "Staff list: invite, deactivate, see who has no second factor."],
      ["schedules", "Schedules", "Doctor schedules (opening hours and slot length)."],
      ["shifts", "Shifts", "Staff shifts, used as evidence for nurse and lab accesses."],
      ["services", "Services", "Billable services and prices."],
      ["consent", "Consent notices", "Consent notice versions in English and Hindi."],
      ["break-glass", "Emergency access", "Break-glass review queue: every emergency access is reviewed within 24 hours."],
      ["patient-reports", "Patient reports", "Accesses that patients said they do not recognise."],
      ["revenue", "Revenue", "Daily revenue."],
      ["audit", "Audit log", "Audit log: hash chain, signed checkpoint and each outside witness, all checked."],
    ];
    for (const [key, label, caption] of tabs) {
      await step(`admin ${key}`, async () => {
        await page.getByRole("tab", { name: label }).click();
        await settle(page);
        await shot(page, "admin", key, caption);
      });
    }
    await step("admin audit-tampered", async () => {
      sql(TAMPER);
      try {
        await page.getByRole("button", { name: "Refresh" }).click();
        await settle(page);
        await page.getByTestId("check-chain").filter({ hasText: "Fail" }).waitFor({ timeout: 120_000 });
        await shot(page, "admin", "audit-tampered", "Tamper demo: one audit row was edited in the database, and the check fails.");
      } finally {
        sql(UNDO);
      }
      await page.getByRole("button", { name: "Refresh" }).click();
      await settle(page);
      await page.getByTestId("check-chain").filter({ hasText: "Pass" }).waitFor({ timeout: 120_000 });
    });
  });
  await page.context().close();

  // Patient ------------------------------------------------------------------------------------
  page = await fresh();
  await step("patient", async () => {
    await patientSignIn(page, "patient1.a@demo.healthsaathi.test");
    await shot(page, "patient", "record", "Patient portal: my record, visits, prescriptions and lab results.", true);
    for (const [label, key, caption] of [
      ["Who opened my record", "access-log", "Access log: every access with who, when and why in plain words."],
      ["Consent", "consent", "Consent: the notice the patient agreed to, and withdrawal."],
      ["Download", "download", "Download my data as JSON or FHIR, and print my record."],
    ] as const) {
      await step(`patient ${key}`, async () => {
        await page.getByRole("tab", { name: new RegExp(label) }).click();
        await settle(page);
        await shot(page, "patient", key, caption);
      });
    }
    await step("patient receipt", async () => {
      await page.getByRole("tab", { name: /Who opened my record/ }).click();
      await settle(page);
      await page.getByRole("button", { name: /Check receipt/ }).first().click();
      await settle(page);
      await page.getByText(/Verified/).first().waitFor({ timeout: 120_000 });
      await shot(page, "patient", "receipt-verified", "Receipt check: the browser verifies the access against the signed checkpoint and the chain.");
    });
    await page.goto(`${BASE}/patient/print`);
    await settle(page);
    await shot(page, "patient", "record-print", "Printable copy of my record.");
  });
  await page.context().close();

  // Dev mailbox (demo only) --------------------------------------------------------------------
  page = await fresh();
  await step("mailbox", async () => {
    await page.goto(`${BASE}/dev/mailbox`);
    await settle(page);
    await shot(page, "demo", "dev-mailbox", "Demo only: the emails the app sent (sign-in codes for demo accounts).");
  });
  await page.context().close();

  writeIndex();
  const viewer = await fresh();
  await viewer.goto(`file:///${join(OUT, "index.html").replace(/\\/g, "/")}`);
  await viewer.waitForLoadState("load");
  await viewer.pdf({ path: join(OUT, "screens.pdf"), format: "A4", printBackground: true, margin: { top: "12mm", bottom: "12mm", left: "10mm", right: "10mm" } });
  await browser.close();

  const pngs = shots.reduce((n, s) => n + s.files.length, 0);
  console.log(`\n${shots.length} screens, ${pngs} PNGs in demo-screenshots/`);
  if (failures.length) {
    console.log("Failed or incomplete:");
    for (const f of failures) {
      console.log(`  ${f}`);
    }
    process.exitCode = 1;
  }
}

const ROLE_TITLES: Record<string, string> = {
  public: "Public screens",
  reception: "Reception",
  nurse: "Nurse",
  doctor: "Doctor",
  lab: "Lab technician",
  admin: "Clinic admin",
  patient: "Patient",
  demo: "Demo tools",
};

function writeIndex(): void {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const sections = Object.keys(ROLE_TITLES)
    .map((role) => {
      const mine = shots.filter((s) => s.role === role && s.files.length);
      if (!mine.length) {
        return "";
      }
      const items = mine
        .map(
          (s) => `<figure><figcaption>${esc(s.caption)}</figcaption><div class="row">${s.files
            .map((f) => `<a href="${f}"><img src="${f}" alt="${esc(f)}" class="${f.includes("-mobile") ? "m" : "d"}"><span>${esc(f)}</span></a>`)
            .join("")}</div></figure>`,
        )
        .join("\n");
      return `<section><h2>${ROLE_TITLES[role]}</h2>\n${items}</section>`;
    })
    .join("\n");
  writeFileSync(
    join(OUT, "index.html"),
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>HealthSaathi screens</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px;color:#111;background:#fff}
h1{margin:0 0 4px} p.note{color:#555;margin:0 0 24px}
section{break-before:page} section:first-of-type{break-before:auto}
figure{margin:0 0 28px;break-inside:avoid} figcaption{font-weight:600;margin-bottom:8px}
.row{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.row a{display:flex;flex-direction:column;gap:4px;text-decoration:none;color:#555;font-size:11px}
img{border:1px solid #ccc;max-height:520px;object-fit:contain;object-position:top}
img.d{width:480px} img.m{width:150px}
</style></head><body><h1>HealthSaathi screens</h1>
<p class="note">Local demo with synthetic data, taken ${new Date().toISOString().slice(0, 16).replace("T", " ")} UTC. Desktop 1440x900 and phone 360x800; "-hi" files are in Hindi.</p>
${sections}</body></html>`,
  );
}

await main();

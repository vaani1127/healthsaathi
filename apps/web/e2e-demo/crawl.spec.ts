import { readFileSync } from "node:fs";

import { expect, type Page, test, type TestInfo } from "@playwright/test";

import { emailCode, fixture, signIn } from "../e2e/support";

// Logs in as every role, opens every route and tab it can reach, and fails on any console error,
// any 4xx or 5xx response, any untranslated i18n key on screen, a loading state that never
// finishes, a raw or generic error message, or a page wider than the screen.

const en = JSON.parse(readFileSync(new URL("../src/locales/en.json", import.meta.url), "utf-8")) as object;

function keys(obj: object, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    typeof v === "object" && v !== null ? keys(v as object, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}
const KEYS = keys(en).filter((k) => k.includes("."));
const GENERIC_ERRORS = [
  "Something went wrong",
  "कुछ गड़बड़",
  "Request is not valid",
  "Internal Server Error",
  "[object Object]",
  "undefined",
  "NaN",
];

interface Problems {
  console: string[];
  responses: string[];
}

function watch(page: Page): Problems {
  const found: Problems = { console: [], responses: [] };
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      found.console.push(`${page.url()}: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => found.console.push(`${page.url()}: ${err.message}`));
  page.on("response", (resp) => {
    if (resp.status() >= 400) {
      found.responses.push(`${resp.status()} ${resp.request().method()} ${resp.url()} (on ${page.url()})`);
    }
  });
  return found;
}

async function hindi(page: Page, info: TestInfo): Promise<boolean> {
  const on = info.project.name.includes("hindi");
  await page.addInitScript((lang) => {
    try {
      localStorage.setItem("hs.lang", lang);
    } catch {
      // ignore
    }
  }, on ? "hi" : "en");
  return on;
}

/** Waits for the page to settle and checks what is on screen. */
async function checkScreen(page: Page, where: string, inHindi: boolean): Promise<void> {
  await page.waitForLoadState("networkidle");
  await expect(page.getByRole("status").filter({ hasText: /^(Loading\.\.\.|लोड हो रहा है)/ }), `${where}: loading never finished`).toHaveCount(0, { timeout: process.env.E2E_NATIVE ? 150_000 : 15_000 });
  const text = await page.locator("body").innerText();
  const leaked = KEYS.filter((k) => text.includes(k));
  expect(leaked, `${where}: untranslated keys on screen`).toEqual([]);
  for (const bad of GENERIC_ERRORS) {
    expect(text.includes(bad), `${where}: shows "${bad}"`).toBe(false);
  }
  if (inHindi) {
    expect(/[ऀ-ॿ]/.test(text), `${where}: no Hindi text in the Hindi run`).toBe(true);
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${where}: wider than the screen`).toBeLessThanOrEqual(1);
}

async function visit(page: Page, path: string, inHindi: boolean): Promise<void> {
  await page.goto(path);
  await checkScreen(page, path, inHindi);
}

async function clickTabs(page: Page, inHindi: boolean): Promise<void> {
  const tabs = page.getByRole("tab");
  const count = await tabs.count();
  for (let i = 0; i < count; i++) {
    await tabs.nth(i).click();
    await checkScreen(page, `${page.url()} tab ${i + 1}`, inHindi);
  }
}

async function followFirst(page: Page, pattern: RegExp, inHindi: boolean): Promise<boolean> {
  const hrefs = await page.locator("a[href]").evaluateAll((els) => els.map((e) => (e as HTMLAnchorElement).getAttribute("href") ?? ""));
  const target = hrefs.find((h) => pattern.test(h));
  if (!target) {
    return false;
  }
  await visit(page, target, inHindi);
  return true;
}

function noProblems(found: Problems): void {
  expect(found.console, "console errors").toEqual([]);
  expect(found.responses, "4xx or 5xx responses").toEqual([]);
}

test("public pages", async ({ page }, info) => {
  const found = watch(page);
  const inHindi = await hindi(page, info);
  for (const path of ["/login", "/patient/login", "/verify", "/status", "/dev/mailbox"]) {
    await visit(page, path, inHindi);
  }
  noProblems(found);
});

const STAFF: [string, RegExp[]][] = [
  ["clinic_admin", []],
  ["reception", [/^\/reception\/patients\//, /^\/reception\/invoices\//, /^\/print\/receipt\//]],
  ["nurse", [/^\/nurse\/patients\//]],
  ["doctor", [/^\/doctor\/patients\//, /^\/print\/prescription\//, /^\/print\/lab\//]],
  ["lab_tech", []],
];

for (const [role, deeper] of STAFF) {
  test(`every screen of ${role}`, async ({ page }, info) => {
    const found = watch(page);
    const inHindi = await hindi(page, info);
    await signIn(page, role);
    const home = new URL(page.url()).pathname;
    await checkScreen(page, home, inHindi);
    await clickTabs(page, inHindi);
    for (const pattern of deeper) {
      await page.goto(home);
      await checkScreen(page, home, inHindi);
      if (!(await followFirst(page, pattern, inHindi))) {
        // Reached through a patient page (prints, invoices).
        await followFirst(page, /^\/(reception|doctor)\/patients\//, inHindi);
        await followFirst(page, pattern, inHindi);
      }
    }
    await visit(page, "/account/sessions", inHindi);
    noProblems(found);
  });
}

test("every screen of a patient", async ({ page }, info) => {
  const found = watch(page);
  const inHindi = await hindi(page, info);
  const patient = (fixture() as unknown as { all: { role: string; email: string; clinic: string }[] }).all.find(
    (u) => u.role === "patient" && u.clinic === "a",
  );
  expect(patient).toBeTruthy();
  const email = patient?.email ?? "";
  await page.goto("/patient/login");
  await page.locator("#p-email").fill(email);
  const since = Date.now() - 2000;
  await page.locator("form button[type=submit]").click();
  let code: string | null = null;
  await expect.poll(async () => (code = await emailCode(email, since)), { timeout: process.env.E2E_NATIVE ? 60_000 : 15_000 }).not.toBeNull();
  await page.locator("#p-code").fill(code ?? "");
  await page.locator("form button[type=submit]").click();
  await expect(page).toHaveURL(/\/patient/);
  await checkScreen(page, "/patient", inHindi);
  await clickTabs(page, inHindi);
  await visit(page, "/patient/print", inHindi);
  await page.goto("/patient");
  await checkScreen(page, "/patient", inHindi);
  for (const pattern of [/^\/print\/lab\//, /^\/print\/receipt\//]) {
    await page.goto("/patient");
    await checkScreen(page, "/patient", inHindi);
    await followFirst(page, pattern, inHindi);
  }
  await visit(page, "/account/sessions", inHindi);
  noProblems(found);
});

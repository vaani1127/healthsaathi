// Full app audit walkthrough (docs/AUDIT_REPORT.md), `make demo-audit` only: never part of
// `make demo-e2e`. Runs against the demo with rate limits on and records, without failing:
//
// - every screen each role reaches (home, tabs, every internal link pattern up to twice, print
//   pages), in English and Hindi, at desktop and 360 px, for clinic A (read only);
// - console errors, 4xx and 5xx responses, untranslated keys, raw error text, a page wider than
//   the screen, and axe (WCAG 2.0/2.1 A and AA) results per screen, with a screenshot;
// - in clinic B, once per role (English, desktop): every button on every screen and tab is
//   clicked, and every request that changes data is logged (method, path, status, button,
//   account), so the data can be reset before a final demo. Buttons in the signed-in admin's own
//   row and sign-out buttons are never clicked.
//
// Results: ../../audit-artifacts/walkthrough/<test>.json and screenshots under audit-artifacts/.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Request, test } from "@playwright/test";

import { emailCode, fixture, signIn } from "../../e2e/support";

const ARTIFACTS = "../../audit-artifacts";
const PACE_MS = 400;
const MAX_SCREENS = 45;
const MAX_CLICKS_PER_SCREEN = 20;

interface Login {
  role: string;
  email: string;
  name: string;
  clinic: string;
  user_id: string;
  totp_secret: string;
}

const en = JSON.parse(readFileSync(new URL("../../src/locales/en.json", import.meta.url), "utf-8")) as object;
function keys(obj: object, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) => (typeof v === "object" && v !== null ? keys(v as object, `${prefix}${k}.`) : [`${prefix}${k}`]));
}
const KEYS = keys(en).filter((k) => k.includes("."));
const RAW_ERRORS = ["Something went wrong", "कुछ गड़बड़", "Request is not valid", "Internal Server Error", "[object Object]", "undefined", "NaN"];
const SKIP_BUTTON = /sign out|log out|साइन आउट|लॉग आउट/i;

interface Screen {
  id: string;
  url: string;
  tab: number | null;
  tabName: string | null;
  screenshot: string;
  console: string[];
  responses: string[];
  rateLimited: string[];
  untranslated: string[];
  rawErrors: string[];
  overflowPx: number;
  noHindi: boolean;
  axe: string[];
  loadingStuck: boolean;
}

interface Click {
  screen: string;
  button: string;
  row: string | null;
  screenshot: string;
  console: string[];
  responses: string[];
  changes: string[];
}

interface Report {
  test: string;
  clinic: string;
  role: string;
  account: string;
  lang: string;
  viewport: string;
  screens: Screen[];
  clicks: Click[];
  errors: string[];
}

function login(role: string, clinic: string): Login {
  const found = (fixture() as unknown as { all: Login[] }).all.find((u) => u.role === role && u.clinic === clinic);
  if (!found) {
    throw new Error(`no ${role} in clinic ${clinic}`);
  }
  return found;
}

function slug(text: string): string {
  return text.replace(/^https?:\/\/[^/]+/, "").replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, "id").replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80) || "home";
}

/** Collects console errors and failed responses between two calls of `take()`. */
function listen(page: Page) {
  let console_: string[] = [];
  let responses: string[] = [];
  let limited: string[] = [];
  let changes: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console_.push(msg.text().slice(0, 300));
    }
  });
  page.on("pageerror", (err) => console_.push(`pageerror: ${err.message.slice(0, 300)}`));
  page.on("response", (resp) => {
    const req: Request = resp.request();
    const path = new URL(resp.url()).pathname;
    if (resp.status() === 429) {
      limited.push(`${req.method()} ${path}`);
    } else if (resp.status() >= 400) {
      responses.push(`${resp.status()} ${req.method()} ${path}`);
    }
    if (req.method() !== "GET" && path.startsWith("/api/v1/") && !path.startsWith("/api/v1/auth/refresh")) {
      changes.push(`${req.method()} ${path} -> ${resp.status()}`);
    }
  });
  return {
    take() {
      const out = { console: console_, responses, limited, changes };
      console_ = [];
      responses = [];
      limited = [];
      changes = [];
      return out;
    },
  };
}

async function settle(page: Page): Promise<boolean> {
  try {
    await page.waitForLoadState("networkidle", { timeout: 15_000 });
  } catch {
    // keep going: recorded as stuck below if a loading line stays
  }
  await page.waitForTimeout(PACE_MS);
  const loading = page.getByRole("status").filter({ hasText: /^(Loading\.\.\.|लोड हो रहा है)/ });
  for (let i = 0; i < 20 && (await loading.count()) > 0; i++) {
    await page.waitForTimeout(500);
  }
  return (await loading.count()) > 0;
}

async function record(page: Page, report: Report, ears: ReturnType<typeof listen>, dir: string, tab: number | null, tabName: string | null): Promise<Screen> {
  const stuck = await settle(page);
  const n = String(report.screens.length + 1).padStart(2, "0");
  const id = `${slug(page.url())}${tabName ? `#${slug(tabName)}` : ""}`;
  const screenshot = `${dir}/${n}-${id}.png`;
  mkdirSync(`${ARTIFACTS}/${dir}`, { recursive: true });
  await page.screenshot({ path: `${ARTIFACTS}/${screenshot}`, fullPage: true });
  const text = await page.locator("body").innerText();
  let axe: string[];
  try {
    const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    axe = result.violations.map((v) => `${v.id} (${v.impact ?? "?"}): ${v.help} -> ${v.nodes.map((x) => x.target.join(" ")).slice(0, 3).join(", ")}`);
  } catch (err) {
    axe = [`axe failed: ${String(err).slice(0, 200)}`];
  }
  const heard = ears.take();
  const screen: Screen = {
    id,
    url: new URL(page.url()).pathname,
    tab,
    tabName,
    screenshot: `audit-artifacts/${screenshot}`,
    console: heard.console,
    responses: heard.responses,
    rateLimited: heard.limited,
    untranslated: KEYS.filter((k) => text.includes(k)),
    rawErrors: RAW_ERRORS.filter((bad) => text.includes(bad)),
    overflowPx: await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
    noHindi: report.lang === "hi" && !/[ऀ-ॿ]/.test(text),
    axe,
    loadingStuck: stuck,
  };
  report.screens.push(screen);
  return screen;
}

function pattern(href: string): string {
  return (href.split("?")[0] ?? href).replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, ":id");
}

async function internalLinks(page: Page): Promise<string[]> {
  const hrefs = await page.locator("a[href]").evaluateAll((els) => els.map((e) => (e as HTMLAnchorElement).getAttribute("href") ?? ""));
  return hrefs.filter((h) => h.startsWith("/") && !h.startsWith("/api/") && !h.startsWith("//"));
}

async function tabNames(page: Page): Promise<string[]> {
  return page.getByRole("tab").allInnerTexts();
}

async function openTab(page: Page, url: string, tab: number | null): Promise<void> {
  await page.goto(url);
  await settle(page);
  if (tab !== null) {
    await page.getByRole("tab").nth(tab).click();
    await settle(page);
  }
}

async function clickEverything(page: Page, report: Report, ears: ReturnType<typeof listen>, dir: string, url: string, tab: number | null, label: string, me: Login): Promise<void> {
  await openTab(page, url, tab);
  ears.take();
  const names: { name: string; row: string | null }[] = [];
  const buttons = page.locator("main button:visible, [role=dialog] button:visible");
  const total = await buttons.count();
  for (let i = 0; i < total; i++) {
    const b = buttons.nth(i);
    const role = await b.getAttribute("role");
    if (role === "tab") {
      continue;
    }
    const name = (await b.innerText()).trim() || (await b.getAttribute("aria-label")) || "";
    const row = await b.evaluate((el) => el.closest("li, tr, article")?.textContent?.trim().slice(0, 120) ?? null);
    if (!name || SKIP_BUTTON.test(name) || (row && row.includes(me.email))) {
      continue;
    }
    names.push({ name, row });
  }
  for (const target of names.slice(0, MAX_CLICKS_PER_SCREEN)) {
    await openTab(page, url, tab);
    ears.take();
    const scope = target.row ? page.locator("li, tr, article").filter({ hasText: target.row.slice(0, 40) }) : page.locator("main");
    const button = scope.locator("button:visible").filter({ hasText: target.name }).first();
    try {
      if (!(await button.isEnabled({ timeout: 2000 }))) {
        continue;
      }
      await button.click({ timeout: 5000 });
    } catch {
      continue;
    }
    await settle(page);
    const shot = `${dir}/click-${String(report.clicks.length + 1).padStart(3, "0")}-${slug(label)}-${slug(target.name)}.png`;
    mkdirSync(`${ARTIFACTS}/${dir}`, { recursive: true });
    await page.screenshot({ path: `${ARTIFACTS}/${shot}`, fullPage: true });
    const heard = ears.take();
    report.clicks.push({
      screen: label,
      button: target.name,
      row: target.row,
      screenshot: `audit-artifacts/${shot}`,
      console: heard.console,
      responses: heard.responses,
      changes: heard.changes,
    });
    await page.keyboard.press("Escape").catch(() => undefined);
  }
}

async function patientSignIn(page: Page, email: string): Promise<void> {
  await page.goto("/patient/login");
  await page.locator("#p-email").fill(email);
  const since = Date.now() - 2000;
  await page.locator("form button[type=submit]").click();
  let code: string | null = null;
  for (let i = 0; i < 40 && !code; i++) {
    code = await emailCode(email, since);
    if (!code) {
      await page.waitForTimeout(500);
    }
  }
  await page.locator("#p-code").fill(code ?? "");
  await page.locator("form button[type=submit]").click();
  await page.waitForURL(/\/patient/);
}

function patientsFile(clinic: string): string {
  return `${ARTIFACTS}/walkthrough/patients-${clinic}.json`;
}

function knownPatients(clinic: string): string[] {
  try {
    return JSON.parse(readFileSync(patientsFile(clinic), "utf-8")) as string[];
  } catch {
    return [];
  }
}

/** Patient ids from the reception search box (two letters start a search). */
async function searchPatients(page: Page): Promise<string[]> {
  mkdirSync(`${ARTIFACTS}/walkthrough`, { recursive: true });
  const box = page.locator("main input").first();
  const ids = new Set<string>();
  for (const term of ["an", "ra"]) {
    await box.fill(term);
    await settle(page);
    for (const href of await internalLinks(page)) {
      const id = /^\/reception\/patients\/([0-9a-f-]{36})/.exec(href)?.[1];
      if (id) {
        ids.add(id);
      }
    }
    if (ids.size >= 3) {
      break;
    }
  }
  await box.fill("");
  return [...ids];
}

async function walk(page: Page, report: Report, me: Login | null, actions: boolean): Promise<void> {
  // Buttons are clicked only for a signed-in clinic B account.
  const clicking = actions && me !== null;
  const ears = listen(page);
  const dir = `${report.clinic}-${report.role}/${report.lang}-${report.viewport}`;
  const start: string[] = [];
  if (me && me.role === "patient") {
    await patientSignIn(page, me.email);
    start.push("/patient", "/patient/print", "/account/sessions");
  } else if (me) {
    await signIn(page, me.role, me);
    start.push(new URL(page.url()).pathname, "/account/sessions");
    // Patient pages are reached through search (reception) or today's queue (nurse, doctor), not
    // through links on the home screen. Reception searches and shares the patients it finds
    // with the later nurse and doctor tests of the same clinic.
    if (me.role === "reception") {
      const found = await searchPatients(page);
      writeFileSync(patientsFile(report.clinic), JSON.stringify(found));
      start.push(...found.slice(0, 3).map((id) => `/reception/patients/${id}`));
    } else if (me.role === "nurse" || me.role === "doctor") {
      start.push(...knownPatients(report.clinic).slice(0, 3).map((id) => `/${me.role}/patients/${id}`));
    }
  } else {
    start.push("/login", "/patient/login", "/verify", "/status", "/dev/mailbox", "/invite", "/bench/receipt", "/select-clinic", "/no-such-page");
  }
  ears.take();
  const queue = [...start];
  const seenPatterns = new Map<string, number>();
  const visited = new Set<string>();
  while (queue.length > 0 && report.screens.length < MAX_SCREENS) {
    const url = queue.shift();
    if (url === undefined) {
      break;
    }
    if (visited.has(url)) {
      continue;
    }
    visited.add(url);
    const p = pattern(url);
    if ((seenPatterns.get(p) ?? 0) >= 2) {
      continue;
    }
    seenPatterns.set(p, (seenPatterns.get(p) ?? 0) + 1);
    try {
      await page.goto(url);
      await record(page, report, ears, dir, null, null);
      const links = await internalLinks(page);
      const names = await tabNames(page);
      if (clicking && me) {
        await clickEverything(page, report, ears, dir, url, null, url, me);
      }
      for (const [t, name] of names.entries()) {
        await openTab(page, url, t);
        await record(page, report, ears, dir, t, name);
        links.push(...(await internalLinks(page)));
        if (clicking && me) {
          await clickEverything(page, report, ears, dir, url, t, `${url}#${name}`, me);
        }
      }
      for (const link of links) {
        if (!visited.has(link) && (seenPatterns.get(pattern(link)) ?? 0) < 2) {
          queue.push(link);
        }
      }
    } catch (err) {
      report.errors.push(`${url}: ${String(err).slice(0, 300)}`);
    }
  }
}

function save(report: Report): void {
  mkdirSync(`${ARTIFACTS}/walkthrough`, { recursive: true });
  writeFileSync(`${ARTIFACTS}/walkthrough/${slug(report.test)}.json`, JSON.stringify(report, null, 2));
}

const ROLES = ["reception", "nurse", "doctor", "lab_tech", "clinic_admin", "patient"];
const VIEWPORTS = { desktop: { width: 1280, height: 800 }, "360": { width: 360, height: 780 } } as const;

test.describe.configure({ mode: "serial" });

for (const lang of ["en", "hi"] as const) {
  for (const [viewport, size] of Object.entries(VIEWPORTS)) {
    const combos: [string, string | null][] = [["public", null], ...ROLES.map((r): [string, string] => [r, r])];
    for (const [label, role] of combos) {
      test(`clinic a ${label} ${lang} ${viewport}`, async ({ browser }) => {
        test.skip(!process.env.DEMO_AUDIT, "run with make demo-audit");
        test.setTimeout(1_200_000);
        const context = await browser.newContext({ viewport: size, baseURL: "http://localhost:8080" });
        await context.addInitScript((l) => {
          try {
            localStorage.setItem("hs.lang", l);
          } catch {
            // ignore
          }
        }, lang);
        const page = await context.newPage();
        const me = role ? login(role, "a") : null;
        const report: Report = { test: `clinic a ${label} ${lang} ${viewport}`, clinic: "a", role: label, account: me?.email ?? "none", lang, viewport, screens: [], clicks: [], errors: [] };
        try {
          await walk(page, report, me, false);
        } catch (err) {
          report.errors.push(`walk: ${String(err).slice(0, 300)}`);
        }
        save(report);
        await context.close();
      });
    }
  }
}

for (const role of ROLES) {
  test(`clinic b ${role} actions en desktop`, async ({ browser }) => {
    test.skip(!process.env.DEMO_AUDIT, "run with make demo-audit");
    test.setTimeout(2_400_000);
    const context = await browser.newContext({ viewport: VIEWPORTS.desktop, baseURL: "http://localhost:8080" });
    await context.addInitScript(() => {
      try {
        localStorage.setItem("hs.lang", "en");
      } catch {
        // ignore
      }
    });
    const page = await context.newPage();
    const me = login(role, "b");
    const report: Report = { test: `clinic b ${role} actions en desktop`, clinic: "b", role, account: me.email, lang: "en", viewport: "desktop", screens: [], clicks: [], errors: [] };
    try {
      await walk(page, report, me, true);
    } catch (err) {
      report.errors.push(`walk: ${String(err).slice(0, 300)}`);
    }
    save(report);
    await context.close();
  });
}

// PWA offline rules (SPEC 4): notes, lab results and prescriptions are never cached, and signing
// out removes the cached API responses and unsent drafts. On the demo's production build, as the
// clinic A doctor and as a clinic A patient: open records with notes, lab results and
// prescriptions, list every Cache Storage cache with its URLs and every IndexedDB store with its
// keys, then sign out and list them again. Results in audit-artifacts/walkthrough/pwa-*.json.
const SENSITIVE = /\/api\/v1\/.*(notes|prescriptions|lab|chart|vitals|allergies|conditions|documents|portal)/;

interface Storage {
  controlled: boolean;
  caches: Record<string, string[]>;
  indexedDB: Record<string, Record<string, string[]>>;
}

async function storage(page: Page): Promise<Storage> {
  return page.evaluate(async () => {
    const out: Storage = { controlled: Boolean(navigator.serviceWorker?.controller), caches: {}, indexedDB: {} };
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      out.caches[name] = (await cache.keys()).map((r) => new URL(r.url).pathname + new URL(r.url).search);
    }
    const dbs = "databases" in indexedDB ? await indexedDB.databases() : [];
    for (const info of dbs) {
      if (!info.name) {
        continue;
      }
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const req = indexedDB.open(info.name ?? "");
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
      const stores: Record<string, string[]> = {};
      for (const store of Array.from(db.objectStoreNames)) {
        stores[store] = await new Promise<string[]>((resolve) => {
          const req = db.transaction(store, "readonly").objectStore(store).getAllKeys();
          req.onsuccess = () => resolve(req.result.map((k) => String(k)));
          req.onerror = () => resolve(["<error>"]);
        });
      }
      db.close();
      out.indexedDB[info.name] = stores;
    }
    return out;
  });
}

function cachedSensitive(s: Storage): string[] {
  return Object.entries(s.caches).flatMap(([name, urls]) => urls.filter((u) => SENSITIVE.test(u)).map((u) => `${name}: ${u}`));
}

for (const role of ["doctor", "patient"] as const) {
  test(`pwa cache check clinic a ${role}`, async ({ browser }) => {
    test.skip(!process.env.DEMO_AUDIT, "run with make demo-audit");
    test.setTimeout(600_000);
    const context = await browser.newContext({ viewport: VIEWPORTS.desktop, baseURL: "http://localhost:8080" });
    const page = await context.newPage();
    const fetched: string[] = [];
    page.on("response", (resp) => {
      const path = new URL(resp.url()).pathname;
      if (resp.request().method() === "GET" && resp.ok() && SENSITIVE.test(path)) {
        fetched.push(path);
      }
    });
    const me = login(role, "a");
    const visited: string[] = [];
    const go = async (url: string) => {
      await page.goto(url);
      await settle(page);
      visited.push(url);
    };
    if (role === "patient") {
      await patientSignIn(page, me.email);
    } else {
      await signIn(page, role, me);
    }
    // The service worker controls the page only after a reload; then the runtime caching applies.
    await page.reload();
    await settle(page);
    const pages = role === "patient" ? ["/patient", "/patient/print"] : knownPatients("a").slice(0, 5).map((id) => `/doctor/patients/${id}`);
    for (const url of pages) {
      await go(url);
      for (const print of (await internalLinks(page)).filter((h) => h.startsWith("/print/")).slice(0, 3)) {
        await go(print);
      }
    }
    const before = await storage(page);
    await page.getByRole("button", { name: /^(Sign out|साइन आउट)$/ }).click();
    await page.waitForURL(/\/login/);
    await settle(page);
    const after = await storage(page);
    const result = {
      role,
      account: me.email,
      visited,
      sensitiveResponsesFetched: [...new Set(fetched)],
      beforeSignOut: before,
      afterSignOut: after,
      cachedSensitiveBefore: cachedSensitive(before),
      apiCachesAfterSignOut: Object.keys(after.caches).filter((n) => n.startsWith("hs-api")),
      draftsAfterSignOut: Object.values(after.indexedDB).flatMap((stores) => Object.values(stores).flat()),
    };
    const verdict =
      result.sensitiveResponsesFetched.length === 0
        ? "inconclusive: no notes, lab or prescription response was fetched"
        : result.cachedSensitiveBefore.length === 0 && result.apiCachesAfterSignOut.length === 0 && result.draftsAfterSignOut.length === 0
          ? "pass"
          : "fail";
    mkdirSync(`${ARTIFACTS}/walkthrough`, { recursive: true });
    writeFileSync(`${ARTIFACTS}/walkthrough/pwa-${role}.json`, JSON.stringify({ verdict, ...result }, null, 2));
    await context.close();
    expect(verdict, JSON.stringify(result.cachedSensitiveBefore)).toBe("pass");
  });
}

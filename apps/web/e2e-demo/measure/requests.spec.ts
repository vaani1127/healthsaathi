// Requests per minute per user on the local demo (`make demo-measure`), for the default rate limit
// (ADR 0003). Six users on one address, each on their busiest screen, act every 5 seconds with a
// full page load for 2 minutes while another account adds a walk-in every 10 seconds, so realtime
// updates make the queues refetch. Writes test-results/measure-requests.json. Not part of
// `make demo-e2e`: it runs only in the "measure" project, which exists only when DEMO_MEASURE=1.
import { mkdirSync, writeFileSync } from "node:fs";

import { type Browser, type Page, test } from "@playwright/test";
import { TOTP } from "otpauth";

import { emailCode, fixture, signIn, user } from "../../e2e/support";

interface Login {
  role: string;
  email: string;
  totp_secret: string;
}

const BASE = "http://localhost:8080";
const MINUTES = 2;
const STEP_MS = 5_000;
const WALKIN_MS = 10_000;
const GENERATOR = "reception2.a@demo.healthsaathi.test";
const PATIENT = "patient1.a@demo.healthsaathi.test";
const REPORT = "test-results/measure-requests.json";

type Hit = { at: number; auth: boolean; path: string };

function login(email: string): Login {
  const found = (fixture() as unknown as { all: Login[] }).all.find((u) => u.email === email);
  if (!found) {
    throw new Error(`no demo login ${email}`);
  }
  return found;
}

async function patientSignIn(page: Page, email: string): Promise<void> {
  await page.goto("/patient/login");
  await page.getByLabel("Email").fill(email);
  const since = Date.now() - 2000;
  await page.getByRole("button", { name: "Send code" }).click();
  let code: string | null = null;
  for (let i = 0; i < 30 && !code; i++) {
    code = await emailCode(email, since);
    if (!code) {
      await page.waitForTimeout(500);
    }
  }
  await page.getByLabel("6 digit code").fill(code ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("heading", { name: /^Hello, / }).waitFor();
}

/** A clinic-scoped access token for the account that generates activity (not one being measured). */
async function apiToken(u: Login): Promise<string> {
  const post = (path: string, body: unknown, token?: string) =>
    fetch(`${BASE}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body),
    });
  const challenge = ((await (await post("/api/v1/auth/login", { email: u.email, password: fixture().password })).json()) as { challenge_token: string }).challenge_token;
  const code = new TOTP({ secret: u.totp_secret, digits: 6, period: 30 }).generate();
  const first = ((await (await post("/api/v1/auth/totp/verify", { challenge_token: challenge, code })).json()) as { access_token: string }).access_token;
  const me = (await (await fetch(`${BASE}/api/v1/auth/me`, { headers: { authorization: `Bearer ${first}` } })).json()) as { memberships: { clinic_id: string; role: string }[] };
  const m = me.memberships[0];
  return ((await (await post("/api/v1/auth/select-clinic", { clinic_id: m?.clinic_id, role: m?.role }, first)).json()) as { access_token: string }).access_token;
}

async function opened(browser: Browser, label: string, open: (page: Page) => Promise<void>, hits: Record<string, Hit[]>): Promise<Page> {
  const context = await browser.newContext({ baseURL: BASE });
  const page = await context.newPage();
  hits[label] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/")) {
      hits[label]?.push({ at: Date.now(), auth: Boolean(r.headers()["authorization"]), path: new URL(r.url()).pathname });
    }
  });
  await open(page);
  return page;
}

/** The most requests inside any 60 second span. */
function busiest(times: number[]): number {
  let peak = 0;
  for (const t of times) {
    peak = Math.max(peak, times.filter((x) => x >= t && x < t + 60_000).length);
  }
  return peak;
}

test("requests per minute per user and without a token", async ({ browser }) => {
  test.skip(!process.env.DEMO_MEASURE, "run with make demo-measure");
  test.setTimeout(900_000);
  const hits: Record<string, Hit[]> = {};
  const signInStart = Date.now();
  const pages = [
    await opened(browser, "reception", async (p) => void (await signIn(p, "reception")), hits),
    await opened(browser, "nurse", async (p) => void (await signIn(p, "nurse")), hits),
    await opened(browser, "doctor", async (p) => void (await signIn(p, "doctor")), hits),
    await opened(browser, "lab_tech", async (p) => void (await signIn(p, "lab_tech")), hits),
    await opened(browser, "clinic_admin", async (p) => {
      await signIn(p, "clinic_admin");
      await p.getByRole("tab", { name: "Audit log" }).click();
    }, hits),
    await opened(browser, "patient", (p) => patientSignIn(p, PATIENT), hits),
  ];
  const signInHits = Object.values(hits).flat();
  const signInNoAuth = signInHits.filter((h) => !h.auth);
  const signInSeconds = Math.round((Date.now() - signInStart) / 1000);
  for (const key of Object.keys(hits)) {
    hits[key] = [];
  }
  const startedAt = Date.now();
  const end = startedAt + MINUTES * 60_000;

  const token = await apiToken(login(GENERATOR));
  const auth = { authorization: `Bearer ${token}`, "content-type": "application/json" };
  const ids = ((await (await fetch(`${BASE}/api/v1/patients?limit=25`, { headers: auth })).json()) as { items: { id: string }[] }).items.map((x) => x.id);
  const staff = (await (await fetch(`${BASE}/api/v1/staff`, { headers: auth })).json()) as { user_id: string; name: string }[];
  const doctorId = staff.find((x) => x.name === user("doctor").name)?.user_id;

  const act = async (page: Page, step: (n: number) => Promise<unknown>) => {
    let n = 0;
    while (Date.now() < end) {
      try {
        await step(n++);
      } catch {
        // A slow screen: carry on with the next step.
      }
      await page.waitForTimeout(STEP_MS);
    }
  };
  const [rec, nur, doc, lab, adm, pat] = pages as [Page, Page, Page, Page, Page, Page];
  const tabs = ["Alerts", "Break-glass", "Audit log"];
  const actions = Promise.all([
    act(rec, (n) => rec.goto(n % 2 ? "/reception" : `/reception/patients/${ids[n % ids.length]}`, { timeout: 8000 })),
    act(nur, (n) => nur.goto(n % 2 ? "/nurse" : `/nurse/patients/${ids[n % ids.length]}`, { timeout: 8000 })),
    act(doc, (n) => doc.goto(n % 2 ? "/doctor" : `/doctor/patients/${ids[n % ids.length]}`, { timeout: 8000 })),
    act(lab, () => lab.goto("/lab", { timeout: 8000 })),
    act(adm, (n) => adm.getByRole("tab", { name: tabs[n % tabs.length] ?? "Alerts" }).click({ timeout: 5000 })),
    act(pat, () => pat.goto("/patient", { timeout: 8000 })),
  ]);
  let walkins = 0;
  let i = 0;
  while (Date.now() < end) {
    const r = await fetch(`${BASE}/api/v1/walkins`, { method: "POST", headers: auth, body: JSON.stringify({ patient_id: ids[i++ % ids.length], doctor_user_id: doctorId }) });
    if (r.ok) {
      walkins++;
    }
    await new Promise((resolve) => setTimeout(resolve, WALKIN_MS));
  }
  await actions;

  const noAuth = Object.values(hits).flat().filter((h) => !h.auth);
  const byPath: Record<string, number> = {};
  for (const h of noAuth) {
    byPath[h.path] = (byPath[h.path] ?? 0) + 1;
  }
  const report = {
    window: [new Date(startedAt).toISOString(), new Date().toISOString()],
    walkins,
    perUserBusiest60s: Object.fromEntries(Object.entries(hits).map(([k, v]) => [k, busiest(v.map((h) => h.at))])),
    perUserTotal: Object.fromEntries(Object.entries(hits).map(([k, v]) => [k, v.length])),
    withoutTokenAllUsers: { total: noAuth.length, busiest60s: busiest(noAuth.map((h) => h.at)), byPath },
    signInPhase: { seconds: signInSeconds, total: signInHits.length, withoutToken: signInNoAuth.length, withoutTokenBusiest60s: busiest(signInNoAuth.map((h) => h.at)) },
  };
  mkdirSync("test-results", { recursive: true });
  writeFileSync(REPORT, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  for (const p of pages) {
    await p.context().close();
  }
});

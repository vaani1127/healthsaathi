import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { expect, type Page } from "@playwright/test";
import { TOTP } from "otpauth";

import { OUTBOX } from "../playwright.config";
import { FIXTURE } from "./global-setup";

/** The same specs run against the dev stack (Vite + API) or the `make demo` stack. */
export const DEMO = process.env.E2E_TARGET === "demo";

/** The language the flows run in: E2E_LANG=hi runs the same flows in Hindi. */
export type Lang = "en" | "hi";
export const LANG: Lang = process.env.E2E_LANG === "hi" ? "hi" : "en";
export const OTHER_LANG: Lang = LANG === "hi" ? "en" : "hi";

const LOCALES: Record<Lang, Record<string, unknown>> = {
  en: JSON.parse(readFileSync(new URL("../src/locales/en.json", import.meta.url), "utf-8")) as Record<string, unknown>,
  hi: JSON.parse(readFileSync(new URL("../src/locales/hi.json", import.meta.url), "utf-8")) as Record<string, unknown>,
};

/** A UI text in the given language, from the app's own locale files, with {{vars}} filled in. */
export function txIn(lang: Lang, key: string, vars: Record<string, string | number> = {}): string {
  let node: unknown = LOCALES[lang];
  for (const part of key.split(".")) {
    node = (node as Record<string, unknown> | undefined)?.[part];
  }
  if (typeof node !== "string") {
    throw new Error(`no ${lang} text for ${key}`);
  }
  return node.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(vars[name] ?? ""));
}

/** A UI text in the language of this run. */
export function tx(key: string, vars: Record<string, string | number> = {}): string {
  return txIn(LANG, key, vars);
}
export const DEMO_FIXTURE = FIXTURE.replace("e2e-fixture.json", "demo-fixture.json");

interface FixtureUser {
  role: string;
  email: string;
  name: string;
  user_id: string;
  totp_secret: string;
}

interface Fixture {
  clinic_id: string;
  password: string;
  users: FixtureUser[];
}

export function fixture(): Fixture {
  return JSON.parse(readFileSync(DEMO ? DEMO_FIXTURE : FIXTURE, "utf-8")) as Fixture;
}

export function user(role: string): FixtureUser {
  const found = fixture().users.find((u) => u.role === role);
  if (!found) {
    throw new Error(`no ${role} in fixture`);
  }
  return found;
}

// Steps already used per secret. Kept in a file because a retry runs in a new worker process,
// while the API remembers every step it has accepted.
const USED = join(dirname(FIXTURE), DEMO ? "demo-totp-steps.json" : "totp-steps.json");

function usedSteps(): Record<string, number> {
  return existsSync(USED) ? (JSON.parse(readFileSync(USED, "utf-8")) as Record<string, number>) : {};
}

/**
 * A TOTP code the server has not seen yet for this user. The server accepts the current step or
 * one either side, each only once, so after many sign-ins in a row this waits for the clock.
 */
async function freshCode(secret: string): Promise<string> {
  const used = usedSteps();
  const step = Math.floor(Date.now() / 30_000);
  const next = Math.max(step, (used[secret] ?? -1) + 1);
  if (next > step + 1) {
    await new Promise((resolve) => setTimeout(resolve, (next - 1) * 30_000 - Date.now() + 500));
  }
  writeFileSync(USED, JSON.stringify({ ...usedSteps(), [secret]: next }));
  return new TOTP({ secret, digits: 6, period: 30 }).generate({ timestamp: next * 30_000 });
}

export async function signIn(page: Page, role: string, who?: FixtureUser): Promise<FixtureUser> {
  const u = who ?? user(role);
  await page.goto("/login");
  // Ids, not labels, so the same sign-in works when the page is in Hindi.
  await page.locator("#email").fill(u.email);
  await page.locator("#password").fill(fixture().password);
  await page.locator("form button[type=submit]").click();
  await page.locator("#code").fill(await freshCode(u.totp_secret));
  await page.locator("form button[type=submit]").click();
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator("header")).toBeVisible();
  return u;
}

/** A sign-in code emailed to this address after `sinceMs`: from the files the test API writes,
 * or from the dev mailbox of the demo stack. */
export async function emailCode(email: string, sinceMs: number, baseURL = "http://localhost:8080"): Promise<string | null> {
  if (DEMO) {
    const resp = await fetch(`${baseURL}/api/v1/dev/mailbox?to=${encodeURIComponent(email)}`);
    const mails = (await resp.json()) as { text: string; sent_at: string | null }[];
    for (const mail of mails) {
      const match = /\b(\d{6})\b/.exec(mail.text);
      if (match?.[1] && mail.sent_at && Date.parse(mail.sent_at) >= sinceMs) {
        return match[1];
      }
    }
    return null;
  }
  const key = `${email.replace("@", "_at_")}.json`;
  const files = readdirSync(OUTBOX)
    .filter((f) => f.endsWith(key) && Number(f.split("-")[0]) / 1e6 >= sinceMs)
    .sort();
  for (const file of files.reverse()) {
    const body = JSON.parse(readFileSync(join(OUTBOX, file), "utf-8")) as { text: string };
    const match = /\b(\d{6})\b/.exec(body.text);
    if (match?.[1]) {
      return match[1];
    }
  }
  return null;
}

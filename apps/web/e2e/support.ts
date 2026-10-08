import { readFileSync } from "node:fs";

import { expect, type Page } from "@playwright/test";
import { TOTP } from "otpauth";

import { FIXTURE } from "./global-setup";

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
  return JSON.parse(readFileSync(FIXTURE, "utf-8")) as Fixture;
}

export function user(role: string): FixtureUser {
  const found = fixture().users.find((u) => u.role === role);
  if (!found) {
    throw new Error(`no ${role} in fixture`);
  }
  return found;
}

const used = new Map<string, number>();

/** A TOTP code the server has not seen yet for this user (each 30 s step works once). */
function freshCode(secret: string): string {
  const totp = new TOTP({ secret, digits: 6, period: 30 });
  const step = Math.floor(Date.now() / 30_000);
  const last = used.get(secret) ?? -1;
  const next = Math.max(step, last + 1);
  used.set(secret, next);
  return totp.generate({ timestamp: next * 30_000 });
}

export async function signIn(page: Page, role: string): Promise<FixtureUser> {
  const u = user(role);
  await page.goto("/login");
  await page.getByLabel("Email").fill(u.email);
  await page.getByLabel("Password").fill(fixture().password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("6 digit code").fill(freshCode(u.totp_secret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return u;
}

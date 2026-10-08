import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

export const OUTBOX = fileURLToPath(new URL("./test-results/outbox", import.meta.url));

// Runs against the local dev stack: the API on :8000 (local Postgres) and Vite on :5173.
// globalSetup creates a fresh clinic and staff for each run.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    viewport: { width: 360, height: 780 },
  },
  projects: [{ name: "mobile-chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 360, height: 780 } } }],
  webServer: [
    {
      command: "uv run uvicorn app.main:app --port 8000",
      cwd: "../api",
      // Many sign-ins from one IP in a minute; the limits themselves are covered by API tests.
      env: { EMAIL_OUTBOX_DIR: OUTBOX, RATE_LIMIT_ENABLED: "false" },
      url: "http://localhost:8000/api/v1/health",
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "pnpm dev --port 5173 --strictPort",
      url: "http://localhost:5173",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});

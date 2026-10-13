import { defineConfig, devices } from "@playwright/test";

// Runs against the `make demo` stack on http://localhost:8080 (production build, API on the same
// origin, anvil chain, local witness). Start it first: `make demo-e2e` does both.
process.env.E2E_TARGET = "demo";

// E2E_LANG=hi opens the app in Hindi (the language the app stores in local storage).
const BASE = "http://localhost:8080";
const language =
  process.env.E2E_LANG === "hi"
    ? { storageState: { cookies: [], origins: [{ origin: BASE, localStorage: [{ name: "hs.lang", value: "hi" }] }] } }
    : {};
const phone = { ...devices["Desktop Chrome"], viewport: { width: 360, height: 780 }, ...language };

export default defineConfig({
  testDir: ".",
  timeout: 90_000,
  globalTimeout: 45 * 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report/demo" }]],
  outputDir: "test-results/demo",
  globalSetup: "./e2e-demo/global-setup.ts",
  // E2E_VIDEO=1 records every test (make demo-videos keeps one video per flow).
  use: { baseURL: BASE, trace: "retain-on-failure", video: process.env.E2E_VIDEO ? "on" : "off" },
  projects: [
    // SPEC 1.3 flows 1 to 9 at 360 px, then the demo-only checks.
    { name: "flows-360", testMatch: ["e2e/*.spec.ts", "e2e-demo/flows/*.spec.ts"], use: phone },
    { name: "crawl-desktop", testMatch: "e2e-demo/crawl.spec.ts", use: { ...devices["Desktop Chrome"] } },
    { name: "crawl-360", testMatch: "e2e-demo/crawl.spec.ts", use: phone },
    { name: "crawl-hindi", testMatch: "e2e-demo/crawl.spec.ts", use: { ...phone, locale: "hi-IN" } },
    { name: "pwa", testMatch: "e2e-demo/pwa.spec.ts", use: phone },
    { name: "a11y", testMatch: "e2e-demo/a11y.spec.ts", use: { ...devices["Desktop Chrome"] } },
    // Rewrites an audit row and puts it back; runs last.
    { name: "tamper", testMatch: "e2e-demo/tamper.spec.ts", use: { ...devices["Desktop Chrome"] }, dependencies: ["flows-360", "crawl-desktop", "crawl-360", "crawl-hindi", "pwa", "a11y"] },
  ],
});

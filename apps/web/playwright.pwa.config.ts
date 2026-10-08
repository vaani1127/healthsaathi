import { defineConfig, devices } from "@playwright/test";

// Installability is checked on the production build (the service worker only exists there).
export default defineConfig({
  testDir: "./e2e-pwa",
  timeout: 60_000,
  reporter: "list",
  use: { baseURL: "http://localhost:4173", ...devices["Desktop Chrome"] },
  webServer: {
    command: "pnpm build && pnpm preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: false,
    timeout: 180_000,
  },
});

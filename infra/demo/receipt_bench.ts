/**
 * Receipt check timing in a real browser on this computer (anchoring study, SPEC 8).
 *
 *     make bench-receipt-browser LABEL="Laptop (Chromium)" [BENCH_URL=https://app.example.org]
 *
 * Opens /bench/receipt, runs it, and saves the timings the page reports to
 * experiments/results/anchoring/browser/<label>.json, the same file the page's "Download timings"
 * button gives on a phone. Without BENCH_URL it serves the production build in apps/web/dist.
 *
 * Runs with Node 24 (TypeScript types are stripped); Playwright comes from apps/web.
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const WEB = join(ROOT, "apps", "web");
const OUT = join(ROOT, "experiments", "results", "anchoring", "browser");
const LABEL = process.env.LABEL ?? "Laptop (Chromium)";
const PORT = 4173;

const require = createRequire(join(WEB, "package.json"));
const { chromium } = require("@playwright/test") as typeof import("@playwright/test");

async function main(): Promise<void> {
  let preview: ReturnType<typeof spawn> | null = null;
  let base = process.env.BENCH_URL;
  if (!base) {
    // Vite's own script, not through a shell: killing a shell would leave the server running.
    const vite = join(WEB, "node_modules", "vite", "bin", "vite.js");
    preview = spawn(process.execPath, [vite, "preview", "--port", String(PORT), "--strictPort"], {
      cwd: WEB,
      stdio: "ignore",
    });
    base = `http://localhost:${PORT}`;
    await new Promise((r) => setTimeout(r, 5000));
  }
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.goto(`${base}/bench/receipt`);
    await page.locator("#bench-label").fill(LABEL);
    await page.getByRole("button", { name: "Start timing" }).click();
    const result = page.getByTestId("bench-result");
    await result.waitFor({ state: "attached", timeout: 600_000 });
    const data = JSON.parse((await result.textContent()) ?? "{}") as { offline: unknown[]; chain: unknown };
    mkdirSync(OUT, { recursive: true });
    const name = LABEL.replace(/[^a-z0-9]+/gi, "-").toLowerCase().replace(/^-|-$/g, "");
    const path = join(OUT, `${name}.json`);
    writeFileSync(path, JSON.stringify(data, null, 2) + "\n");
    console.log(`wrote ${path}`);
    console.log(JSON.stringify({ offline: data.offline, chain: data.chain }));
  } finally {
    await browser.close();
    preview?.kill();
  }
}

await main();
process.exit(0);

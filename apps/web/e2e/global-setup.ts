import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const FIXTURE = fileURLToPath(new URL("../test-results/e2e-fixture.json", import.meta.url));

export default function globalSetup(): void {
  execFileSync("uv", ["run", "python", "-m", "app.scripts.e2e_fixture", FIXTURE], {
    cwd: fileURLToPath(new URL("../../api", import.meta.url)),
    stdio: "inherit",
  });
}

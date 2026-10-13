import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { DEMO_FIXTURE } from "../e2e/support";

export const COMPOSE = ["compose", "-f", "../../infra/docker-compose.demo.yml"];

interface DemoUser {
  clinic: string;
  role: string;
  name: string;
  email: string;
  totp_secret: string | null;
}

/** Reads the demo logins from the running `make demo` stack. The flow specs use the first login
 * of each role in clinic A, in the same shape as the dev-stack fixture. */
export default function globalSetup(): void {
  // Native mode (make demo-native) keeps the demo state in .demo/state of the repository.
  const raw = process.env.E2E_NATIVE
    ? readFileSync(new URL("../../../.demo/state/logins.json", import.meta.url), "utf-8")
    : execFileSync("docker", [...COMPOSE, "exec", "-T", "api", "cat", "/state/logins.json"], {
        encoding: "utf-8",
        env: { ...process.env, MSYS_NO_PATHCONV: "1" },
      });
  const data = JSON.parse(raw) as { password: string; users: DemoUser[] };
  const seen = new Set<string>();
  const users = data.users
    .filter((u) => u.clinic === "a" && !seen.has(u.role) && seen.add(u.role))
    .map((u) => ({ role: u.role, email: u.email, name: u.name, user_id: "", totp_secret: u.totp_secret ?? "" }));
  mkdirSync(dirname(DEMO_FIXTURE), { recursive: true });
  writeFileSync(DEMO_FIXTURE, JSON.stringify({ clinic_id: "", password: data.password, users, all: data.users }));
}

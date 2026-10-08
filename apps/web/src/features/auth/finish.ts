import type { Schemas } from "@/lib/api/client";
import { homeFor, useAuth } from "@/stores/auth";

/** After a session starts: pick the clinic automatically when there is only one choice. */
export async function finishSignIn(token: Schemas["TokenResponse"]): Promise<string> {
  const auth = useAuth.getState();
  await auth.acceptToken(token);
  const memberships = useAuth.getState().me?.memberships ?? [];
  if (memberships.length === 1) {
    const only = memberships[0];
    if (only) {
      await auth.selectClinic(only.clinic_id, only.role);
      return homeFor(only.role);
    }
  }
  return "/select-clinic";
}

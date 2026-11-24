/** The link from the reset email: `/reset-password#email=...&token=...`. The token is in the
 * fragment, which browsers never send to a server (ADR 0010). */
export function readResetLink(hash: string): { email: string; token: string } {
  const fields = new URLSearchParams(hash.replace(/^#/, ""));
  return { email: fields.get("email") ?? "", token: fields.get("token") ?? "" };
}

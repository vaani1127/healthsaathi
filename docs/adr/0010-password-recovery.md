# ADR 0010: Staff password recovery

Status: accepted (24 November 2026)

## Context

Staff sign in with a password and an authenticator code (TOTP). A staff member who forgot their
password could only ask the platform team for help (`docs/SECURITY_CHECKLIST.md` 2.5.6). The
checklist kept self-service recovery out on purpose: a recovery flow by email alone would let
anyone who can read the person's mailbox take over the account, which would make the email the
weakest part of a login that otherwise needs two factors. SPEC 1.2 does not list recovery, and
SPEC 4 lists every rate limit, so adding it departs from SPEC (see the CHANGELOG Deviations
entry of 24 November 2026).

Patients are not affected: they have no password and sign in with a code sent by email.

## Decision

**Asking for a link.** "Forgot password?" on the staff sign-in page leads to
`POST /api/v1/auth/password-reset/request {email}`.

- The answer is always 202 with the same body. It is sent straight after the per-IP limit check;
  the per-email limit, the user lookup, the row lock, the new link, the audit events and the email
  all run in a background task after the response. So the answer and its timing are the same for
  an unknown email, a patient, an invited person without a password and a real staff member.
- A link is sent only to an active user with a password and at least one active staff
  membership.
- The token is 32 random bytes (`secrets.token_urlsafe(32)`), valid for 30 minutes, single use.
  It is stored only as HMAC-SHA256 under its own derived key, label
  `healthsaathi/password-reset/v1` (`app/core/keys.py`, added to the ADR 0009 table), in
  `email_otps` with purpose `password_reset`. No new table or column.
- A new link makes every older open link for that email stop working. The user row is locked
  while this happens, so two requests at once leave exactly one valid link.

**The link holds the token in its fragment.** The email links to
`/reset-password#email=...&token=...`. Browsers do not send the part after `#` to any server, so
the token never reaches the web host, a proxy, or an access log, and is not in a `Referer`
header. The page reads the fragment once and removes it with `history.replaceState`, so it is
not left in the address bar or the browser history. (Invite links keep their query form; they
are not changed here.)

**Setting the new password needs the authenticator too.**
`POST /api/v1/auth/password-reset/confirm {email, token, password, code}`.

- If the person has an authenticator, the request must carry a current code. It is checked with
  the same single-use step claim as signing in (`identity/service.py` `_claim_step`, migration
  0006) and counts towards the same attempt limit (5 per user in 15 minutes). A wrong or reused
  code gets the same 400 `invalid-reset-link` as a bad link, and the link stays unused. A code
  used for a reset cannot be used again to sign in. A missing code gets 400
  `authenticator-code-required`, and the link stays unused; this answer needs a valid link, so it
  says nothing to someone without one.
- So a reset link alone, from someone who can read the mailbox, is not enough for an account
  with an authenticator. A person without an authenticator (never set one up) resets with the
  link alone, and is sent to the authenticator setup at the next sign-in, as before.
- Everything runs in one transaction with the link row locked: two requests with one link at
  once succeed once. When anything is refused the transaction rolls back, so the link stays
  unused and the authenticator step stays unclaimed.
- After a reset: every session of the person is signed out (refresh tokens and access tokens
  stop working), the password lockout counter for the email is cleared, every other open reset
  link is cancelled, and an email says the password was changed and to contact the clinic admin
  if it was not them. A reset never signs anyone in; the next sign-in asks for the
  authenticator as always.

**Limits** (database counters, `rate_counters`): 5 a minute per IP for each endpoint (429), and
3 an hour per email for links. Over the per-email limit the request still answers 202 and sends
nothing.

**Audit.** One event of kind `staff` per request that sends a link and per completed reset:
`{type: "password_reset", action: "requested" | "completed", at, user_id}`. It is written to the
chain of every clinic where the person has an active staff membership, in the same transaction.
It holds no email, token, password or address. A request for an unknown email writes no event:
there is no clinic chain to put it in, and keeping whatever was typed would store personal data
for no use.

## Consequences

- Staff can recover without the platform team, and an account with an authenticator still needs
  the authenticator to change its password.
- Someone who can read a person's mailbox can reset the password of an account that never set up
  an authenticator. Such an account cannot see patient data until the authenticator is set up,
  and the setup happens at the next sign-in.
- A lost authenticator is not covered: the clinic admin and the platform team handle it, as now.
- The reset email goes to the address on the account. In the hosted demo, staff logins use the
  reserved `.test` domain, so their reset emails are not delivered.
- The background task runs in the API process after the response. If that process stops before
  the task runs, no link is sent; the person asks again.

# ADR 0003: Sign-in, sessions and MFA

Status: accepted

## Context

SPEC section 4 requires Argon2id passwords, TOTP for every staff role before patient data, email
codes for patients, 15 minute access tokens and rotating refresh tokens with reuse detection. The
data model in SPEC 3.1 has no column that says whether a session passed MFA, and changing the model
needs agreement, so the design below works with the existing tables.

## Decision

**No staff session without TOTP.** `POST /auth/login` checks the password and returns only a
5 minute challenge token (or a 10 minute enrolment token if the user has no TOTP yet). The session
row and the refresh cookie are created only after `POST /auth/totp/verify` or
`POST /auth/totp/activate`. Patients get a session from `POST /auth/otp/verify` with an emailed
6 digit code. Patient-only accounts cannot use password login.

**Session kind inside the refresh token.** Refresh tokens look like `mfa.<random>` or
`email.<random>`. The database stores the SHA-256 of the whole string, so the kind cannot be changed
by the client. On refresh, the kind decides the `amr` claim (`["pwd", "otp"]` or `["email_otp"]`).
An email code session can never take a staff role.

**Rotation and reuse.** Every refresh marks the presented row revoked and inserts a new row with the
same `family_id`. If a revoked token is presented again, every row of that family is revoked. The
access token's `sid` claim is the `family_id`, and every authenticated request checks that the
family is still active, so sign out and reuse detection take effect at once rather than after 15
minutes.

**Clinic scope.** Tokens start with no clinic. `POST /auth/select-clinic` (or the refresh body)
checks for an active membership with that role and issues a token with `clinic_id` and `role`. The
patient data gate rejects staff tokens without `otp` in `amr`.

**Cookie.** `hs_refresh` is `HttpOnly`, `Secure`, `SameSite=Lax`, path `/api/v1/auth`, with the
domain from `COOKIE_DOMAIN` so `app.` and `api.` subdomains share it.

**Limits kept in memory.** SPEC rate limits use slowapi with in-memory storage. Account lockout
(5 wrong passwords in 15 minutes) and the TOTP attempt limit (5 per user in 15 minutes) use the
same storage. The API runs 2 Uvicorn workers, so each of these is counted per worker (up to twice
the stated number), and a restart clears them (`docs/APP_GAPS.md` 31).

**Each TOTP step once, in the database (amended 17 November 2026).** The last accepted 30 second
step per user is kept in `mfa_secrets.last_step` (migration 0006). A code is accepted only if one
`UPDATE mfa_secrets SET last_step = :step WHERE user_id = :user AND (last_step IS NULL OR
last_step < :step)` changes the row, so two requests with the same code on different workers get
exactly one session, and a restart forgets nothing. The step is committed before the session is
created; if creating the session then fails, the step stays used and the user signs in with the
next code. Starting enrolment again with a new secret clears `last_step`. This replaces the
earlier in-memory replay check, which refused a second use only within one process.

**Secrets.** `JWT_SECRET`, `OTP_HMAC_KEY` and `DATA_KEYS` come from the environment. `make env`
creates a local `.env` with random values. Email codes and invite tokens are stored as HMAC-SHA256
with `OTP_HMAC_KEY`; TOTP secrets are stored with AES-256-GCM using a key id prefix for rotation.

## Consequences

- A stolen password alone never yields a session.
- Lockout counters and rate limit counts are lost on restart and counted per worker, which
  weakens those checks (up to twice the stated limits); Argon2id still applies.
- A TOTP code works once per user whatever the number of workers, and also after a restart.
- Platform admin sign-in is not handled yet; it will be added with the clinic onboarding flow.

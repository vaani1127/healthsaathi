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
(5 wrong passwords in 15 minutes) and TOTP replay protection (each 30 second step accepted once per
user) use the same in-memory approach. This matches the single VM deployment; a restart clears them.

**Secrets.** `JWT_SECRET`, `OTP_HMAC_KEY` and `DATA_KEYS` come from the environment. `make env`
creates a local `.env` with random values. Email codes and invite tokens are stored as HMAC-SHA256
with `OTP_HMAC_KEY`; TOTP secrets are stored with AES-256-GCM using a key id prefix for rotation.

## Consequences

- A stolen password alone never yields a session.
- Lockout counters and TOTP replay state are lost on restart, which briefly weakens those two
  checks; rate limits and Argon2id still apply.
- Platform admin sign-in is not handled yet; it will be added with the clinic onboarding flow.

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

**Sign-in limits in the database (amended 17 November 2026).** Per IP, counted per route:
20 a minute for password sign-in and for the authenticator check, because staff of one clinic
often share one router address (SPEC 4 said 5; see the Deviations in `docs/CHANGELOG.md`), and
5 a minute for email code requests, email code checks and password changes. Per address and
user: email code requests (3 a minute), authenticator
attempts per user (5 in 15 minutes) and the password lockout are fixed-window counters in
`rate_counters` (migration 0007, `core/counters.py`). Each hit is one
`INSERT ... ON CONFLICT ... DO UPDATE SET count = count + 1 RETURNING count`, so both API workers
share the counts and a restart keeps them. Windows are on the database clock. Keys are
HMAC-SHA256 with a key derived for this use, HMAC(`OTP_HMAC_KEY`, "rate-counters-v1"), so no IP
or email is stored. Without the database the limit refuses the request (503). The worker deletes
ended windows every minute. A fixed window lets up to twice a limit through in a short burst
across a window edge; a weighted two-window count was considered and left out, because the
long-run rate is the same and the burst is small next to Argon2id and a 10 character minimum.

**Lockout counts first.** Every password attempt adds one to the email's counter before the
password is checked. Over 5 in the window, the attempt is refused as locked without checking the
password. A wrong password keeps the count; a correct one deletes it. Attempts sent at the same
moment cannot all get in under the limit, because each gets its own count from one statement.

**Email code requests over the per-address limit answer 202** and send nothing, like a request
for an unknown address, so the limit does not reveal which emails exist. The per-IP limit answers
429.

**Client IP.** The app applies the proxy headers itself and believes X-Forwarded-For only from
`TRUSTED_PROXY_IPS`, Caddy's fixed address on a fixed compose subnet; uvicorn runs with
`--no-proxy-headers`. "*" is refused.

**Route limits kept in memory.** Search, export and print limits stay in slowapi's memory: per
worker (up to twice the limit) and cleared on restart.

**Default limit (amended 18 November 2026).** 120 requests a minute for every API request, in a
small ASGI middleware (`DefaultRateLimitMiddleware`), because slowapi's middleware could not find
the routes inside FastAPI 0.142's included routers and limited nothing. The key is the user for
a valid token of type "access"; with no token, an invalid one, a login challenge or an enrolment
token it is the client IP. Health checks, WebSockets and OPTIONS are not counted. It sits inside
the CORS middleware so a 429 still carries the CORS headers. In memory, per worker, like the
route limits: a Postgres counter would add a database write to every request. Every 429 has a
Retry-After header.

Measured on the local demo with six users (reception, nurse, doctor, lab, admin, patient) on one
address, each acting every 5 s with a full page load for 2 minutes while walk-ins arrived every
10 s: the busiest user made 90 requests in its busiest 60 s (reception), and requests without a
token from that one address (all `/auth/refresh`, one per page load) reached 60 in the busiest
60 s, with 12 more in the 19 s of signing everyone in. Both are under 120. The run was a
temporary Playwright script, not kept in the repository, so these numbers are not reproducible
from a script in the repo.

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
- Sign-in limits and the lockout hold across workers and restarts. Login needs the database
  anyway, so failing closed costs nothing extra.
- The search, export and print limits are still per worker and lost on restart.
- A TOTP code works once per user whatever the number of workers, and also after a restart.
- Platform admin sign-in is not handled yet; it will be added with the clinic onboarding flow.

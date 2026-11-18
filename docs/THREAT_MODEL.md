# Threat model

Scope: the HealthSaathi app as built in this repository (PWA, FastAPI API, background worker,
PostgreSQL, anchoring to public witnesses). Method: list the assets, the people who could harm
them, the trust boundaries, then threats per boundary using STRIDE, each with the control that
addresses it and where that control lives. Controls marked "residual" are risks that remain.

All data in this project is synthetic. The design is meant to support obligations under the DPDP
Act 2023 and DPDP Rules 2025; it is not legally reviewed or certified.

## 1. Assets

| Asset | Why it matters |
|---|---|
| Patient records (demographics, vitals, allergies, notes, prescriptions, lab results, files) | Health data; disclosure harms the patient |
| The access log (`access_events`, `access_explanations`, `audit_events`) | The patient's and the auditor's view of who opened what; its value depends on it being complete and unaltered |
| Signed checkpoints and their anchors | Make later edits to the log detectable by people outside the clinic |
| Credentials: passwords, TOTP secrets, refresh tokens, email codes | Give access to everything above |
| Server secrets: `JWT_SECRET`, `DATA_KEYS`, `OTP_HMAC_KEY`, `LEDGER_SIGNING_SEED`, poster wallet key, VAPID key | Forge sessions, decrypt notes, sign false checkpoints |
| Alert reviews and break-glass reviews | Decide whether misuse is acted on |

## 2. Actors

| Actor | Capability assumed |
|---|---|
| Outside attacker | Reaches the public web app and API over the internet; no account |
| Patient | Has their own account; may try to read others' records |
| Curious or malicious staff member (the insider) | Has a valid staff account and role in one clinic; the main actor of the research (SPEC 7: snooping, overreach, bulk export, credential sharing, break-glass abuse, edit after sign, explanation forgery) |
| Clinic admin | Manages staff and reviews alerts; may want to hide their own or a colleague's access |
| Database or server operator | Has owner rights on the database and the VM; could edit rows directly |
| Witness operator (GitHub, chain, OpenTimestamps) | Sees only what is published to them |

## 3. Trust boundaries

1. Browser to API (HTTPS; Caddy in front of the API).
2. API to PostgreSQL (the API connects with a login that switches to `app_rw` per transaction).
3. Worker and anchor job to PostgreSQL (`app_rw` and `anchor_job` roles).
4. Anchor job to public witnesses (Polygon Amoy RPC, GitHub API, OpenTimestamps calendars).
5. API to email (Brevo) and web push services.
6. Patient browser to the public chain (receipt verification reads the chain directly).

## 4. Threats and controls

### Spoofing

| Threat | Control | Where |
|---|---|---|
| Password guessing | Argon2id hashes, 10 character minimum and a common-password list, 5 attempts per 15 minutes lock the account (counted before the password is checked, cleared by a correct password), 20 logins per minute per IP (staff behind one clinic router share an address; the per-account lockout is what limits guessing one account). Both are counted in the database, shared by the API workers and kept across restarts. The IP is the client's: X-Forwarded-For is believed only from Caddy's fixed address. Residual: fixed windows let up to twice a limit through in a short burst across a window edge; anyone who knows an email can lock that account for 15 minutes | `identity/passwords.py`, `password_policy.py`, `lockout.py`, `core/ratelimit.py` |
| Stolen staff password | TOTP is required for every staff role before any patient data (amr `otp` checked on clinic selection) | `identity/service.py` `_check_membership` |
| Replaying an authenticator code seen over someone's shoulder or intercepted | A code is valid for its 30 second step and one step either side, and each step is accepted once per user: the last accepted step is kept in the database and taken with one atomic update, so a second use is refused on any worker and after a restart. An attacker who uses a seen code before its owner gets in only with the password too | `identity/totp.py`, `identity/service.py` `_claim_step` |
| Patient account takeover by guessing codes | 6 digit email code, 10 minutes, 5 attempts per code (counted on the code's row under a row lock), 3 requests per minute per email and 5 per minute per IP, counted in the database. Over the per-email limit the answer is still 202, so the limit does not reveal which emails exist. Residual: the fixed-window edge (up to twice a limit in a short burst) | `identity/service.py`, `core/ratelimit.py` |
| Stolen refresh token | Stored hashed, rotated on every use, reuse of an old token revokes the whole family; HttpOnly, Secure, SameSite=Lax cookie | `identity/service.py`, `identity/router.py` |
| Session on an unknown device | New-device email alert; "My sessions" page lists and revokes sessions | `identity/service.py`, `SessionsPage.tsx` |
| Shared staff login (SPEC 7 type 7) | Not prevented; device and session features feed the detector | `e2d-core` features (residual) |

### Tampering

| Threat | Control | Where |
|---|---|---|
| Editing or deleting audit rows through the app | `app_rw` has INSERT only on `audit_events`, `access_events`, `access_explanations`; append-only trigger | migration `0001`, `tests/db/test_roles.py` |
| Database owner rewrites the audit log | Hash chain per clinic, signed Merkle checkpoints, anchors on a public chain and a public git witness; the audit page and every receipt check the log against them and show FAIL | `ledger/`, `anchor/`, `AuditStatus.tsx`, `e2e-demo/tamper.spec.ts` |
| Rewriting history and re-signing | The signing key is derived from `LEDGER_SIGNING_SEED`; anchored roots on the chain cannot be replaced by the operator; the anchor job refuses to anchor a log that no longer matches the anchored root | `anchor/job.py` `anchor_on_chain` |
| Changing a signed note or prescription | Signed rows are immutable (trigger); edits create a new version | migration `0002`, `clinical/service.py` |
| Forged workflow evidence to make an access look explained (SPEC 7 type 10) | Forgery indicators in the explanation engine, scored by the detector | `e2d-core` (research result H4) |
| Changing workflow rows after an access so that a later look at it finds different evidence, to escape the late forgery flags (no progress, cancelled after access) | The daily job computes the late flags for the evidence the stored explanation named when the access happened, as of 24 hours later, without choosing the template again; if that record cannot be found the flags stay undecided and the access id is in the day's `detect_daily` audit event. Residual: queue tokens and shifts have no status history (`docs/APP_GAPS.md` 24) | `detect/daily.py` |
| Cancelling the evidence in the same request that records an access (for example the request that cancels an appointment also records a view of that patient) | Evidence and status rows are stamped at statement time (`clock_timestamp()`, migration 0005), so the cancellation is dated after the access recorded before it in that request, and that access gets `cancelled_after_access` (`tests/db/test_statement_time.py`). Residual: rows written before migration 0005 keep transaction-time stamps | `app/db/base.py`, `app/db/models/workflow.py` |
| Editing the daily feature cache in `MODEL_DIR` to change a final model | The cache is never the source of truth: its sha256 is in each daily audit event, and a missing or unreadable file is rebuilt from the database. Residual: a cache edited consistently by someone with server access changes the next final models until it is rebuilt | `detect/daily.py` |
| Malicious file upload | PDF, PNG or JPEG only, checked by declared type and file signature, 10 MB limit, stored with its SHA-256 and served as an attachment with `nosniff` | `clinical/service.py`, `clinical/router.py` |

### Repudiation

| Threat | Control | Where |
|---|---|---|
| Staff deny opening a record | Every patient data read and write goes through `record_and_explain()`, in the same transaction, with user, role, session, device and time | `access/service.py` |
| Admin hides a review of their own alert | Reviews of one's own alert or own break-glass are refused | `detect/service.py`, `chart/service.py` |
| Denied attempts disappear with the rolled-back transaction | Denials are written in a separate transaction | `access/service.py` `log_denial` |

### Information disclosure

| Threat | Control | Where |
|---|---|---|
| A user of clinic A reads clinic B | Row level security on every clinic table, set per transaction; a test per table | migration `0001`, `tests/db/test_rls.py` |
| A role reads more than its task needs | Policy module (role x resource x action) and task-view response schemas per role | `access/policy.yaml`, `chart/schemas.py` |
| Notes and TOTP secrets leaked from a database copy | AES-256-GCM with a key id prefix, key from the environment | `core/crypto.py` |
| Patient data on a witness | Only signed tree heads, roots and sizes are published; push messages carry no patient data | `anchor/`, `notify/push.py` |
| Data cached on a shared device | Only the app shell and today's queue are cached; vitals drafts are encrypted with an in-memory key; caches are cleared on sign-out | `vite.config.ts`, `lib/drafts.ts`, `stores/auth.ts` |
| Verbose errors | RFC 7807 problem responses with codes; unhandled errors return `internal-error` only; the UI shows translated messages, never server text | `core/errors.py`, `lib/format.ts` |
| Dev mailbox shows sign-in codes | Answers only when `APP_ENV=local`; the demo binds to 127.0.0.1 | `notify/router.py`, `docker-compose.demo.yml` |

### Denial of service

| Threat | Control | Where |
|---|---|---|
| Request floods | 120 requests per minute per user (valid access token) or per client IP for every API request except health checks, WebSockets and OPTIONS; search, export and print have lower limits. In memory: counted per API process, so with 2 workers up to twice the limit, and cleared on restart. Requests without a token from one clinic share the per-IP count (measured: 60 in the busiest minute for six users reloading every 5 s, ADR 0003) | `core/ratelimit.py` `DefaultRateLimitMiddleware` |
| Large uploads | 10 MB limit | `core/config.py` |
| Witness outage | Each witness is independent; a failed one does not stop the others; the worker runs the job if the scheduler is late | `anchor/job.py`, `worker.py` |
| Rate limits are per process | Sign-in limits and the lockout are in the database. Residual: the search, export and print limits are in memory, reset on restart and are not shared between Uvicorn workers | accepted for a single-VM deployment |
| Staging client sets its own IP with X-Forwarded-For | Accepted: Render staging trusts the header from any peer (`render.yaml`), but it holds synthetic data only and has no real users; prod-demo believes the header only from Caddy (`docs/APP_GAPS.md` 33) | `render.yaml` |
| A client sets its own IP with X-Forwarded-For to skip per-IP limits | The header is believed only from Caddy's fixed address (`TRUSTED_PROXY_IPS`, `10.250.0.2`); Caddy replaces any value the client sent. From any other peer the header is ignored | `main.py`, `docker-compose.prod.yml` |

### Elevation of privilege

| Threat | Control | Where |
|---|---|---|
| Patient opens another patient's record | Patient-self rule in `record_and_explain()` | `access/service.py` |
| Staff without an explanation opens a chart | Doctors must give a typed reason (logged, scored); other roles are limited by policy | `chart/service.py` |
| Break-glass abuse (SPEC 7 type 8) | Mandatory reason code and text, 4 hour window, every use in the 24 hour review queue | `chart/service.py`, `AdminHome.tsx` |
| ML grants access by mistake | It cannot: authorization is the policy module and RLS; the detector only ranks for review | `access/`, `detect/` |

## 5. Residual risks

- Sign-in limits use fixed windows: up to twice a limit can get through in a short burst across
  a window edge.
- The default, search, export and print limits are counted in each API process's memory (up to
  twice the limit with 2 workers, cleared on restart).
- Per-IP limits trust Caddy's X-Forwarded-For. If the API's DNS record were proxied through a CDN,
  every user would share the CDN's few addresses; it must stay DNS only (`docs/RUNBOOK.md`).
- An insider with a genuine care relationship (on the care team, or treating the patient) is
  explained by that relationship; the research results show this is the main source of false
  explanations (`docs/results-v1.md`, section 6).
- Rows written before migration 0005 keep transaction-time stamps, so for that older data an
  access recorded in the same request as a cancellation of its own evidence does not get
  `cancelled_after_access`, and a row created after an access in the same request looks older
  than the access.
- Final alerts come 24 to 48 hours after the access (ADR 0008); until then only the provisional
  alerts exist.
- Anchoring proves the log was not changed after a checkpoint; events written between two
  checkpoints are protected only by the hash chain until the next anchor (15 minutes in the
  deployment design, 2 minutes in the local demo).
- The server operator holds `LEDGER_SIGNING_SEED`; a compromised operator could sign a new
  history for events that were never anchored.
- Email codes depend on the security of the patient's mailbox.

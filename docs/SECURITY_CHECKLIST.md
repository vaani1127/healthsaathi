# Security checklist (OWASP ASVS 4.0.3, level 1)

Every level 1 requirement of ASVS 4.0.3, with Pass, Accepted (with the reason) or N/A (the app
has no such feature). Evidence points to the code or test that shows it. Checked against the code
in this repository and the local demo stack (`make demo`); the scan results are in section 15.

Summary: no open failures. Items marked Accepted are deliberate design decisions, explained in
the row.

## V2 Authentication

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 2.1.1 | Passwords at least 12 characters | Accepted | SPEC 4 sets a minimum of 10. Staff also need a TOTP code for any patient data; patients have no passwords. `identity/password_policy.py` |
| 2.1.2 | Passwords of 64 or more characters allowed | Pass | Up to 256 characters, `identity/schemas.py` |
| 2.1.3 | No truncation | Pass | Argon2id hashes the whole password |
| 2.1.4 | Any printable Unicode allowed | Pass | No character rules |
| 2.1.5 | Users can change their password | Pass | `POST /auth/password`, account page; `test_change_password_needs_the_current_one_and_ends_other_sessions` |
| 2.1.6 | Change needs the current and the new password | Pass | Same endpoint |
| 2.1.7 | Passwords checked against breached passwords | Accepted | Checked against a bundled list of common passwords only. An online breach lookup would send password hash prefixes to a third party, which the zero-cost, local-first design avoids; TOTP limits the damage of a reused password |
| 2.1.8 | Password strength meter | Accepted | The form states the rule and gives a translated message when it is not met; no meter |
| 2.1.9 | No composition rules | Pass | Length and common-list only |
| 2.1.10 | No periodic rotation or history rules | Pass | None |
| 2.1.11 | Paste and password managers allowed | Pass | Standard inputs with `autocomplete` |
| 2.1.12 | Option to show the password | Pass | `components/ui/password.tsx` on every password field |
| 2.2.1 | Anti-automation | Pass | Per IP: 20 password sign-ins and 20 authenticator checks per minute (staff of one clinic often share one router address), and 5 per minute each for email code requests, email code checks and password changes, each counted separately. Per account: 3 email codes per minute per address, 5 authenticator code attempts per user in 15 minutes, and the password lockout (each attempt is counted before the password is checked; over 5 in 15 minutes the account is refused as locked; a correct password clears the count). These are counted in Postgres (`rate_counters`, migration 0007, `core/counters.py`): one atomic statement per attempt, shared by both API workers and kept across restarts. If the database cannot be reached the request is refused (503). Over the per-address limit an email code request still answers 202, so it does not reveal which emails exist. The client IP is taken from X-Forwarded-For only when the request comes from Caddy's fixed address (`TRUSTED_PROXY_IPS`). Fixed windows: up to twice a limit can get through in a short burst across a window edge (the end of one window and the start of the next); over any longer stretch the rate stays at the limit. The 5 attempts allowed per email code are counted on the code's row. The coarse per-route limits (search 30, export and print 10 per minute) stay in each process's memory. The default limit, 120 requests per minute, applies to every other API request (`DefaultRateLimitMiddleware`, `core/ratelimit.py`): per user for a valid access token, else per client IP; health checks, WebSockets and OPTIONS are not counted. These in-memory limits are per worker, so up to twice the limit, and cleared on restart. Every 429 carries Retry-After |
| 2.2.2 | Email and SMS only as a secondary factor | Accepted | Patients sign in with an email code alone (SPEC 1.1). A patient account only reaches that patient's own record, the code expires in 10 minutes and allows 5 attempts |
| 2.2.3 | Notify after changes to authentication details | Pass | Emails on a new device and on a password change |
| 2.3.1 | Initial secrets random, at least 6 characters, expire | Pass | Invite tokens are random and expire after 72 hours |
| 2.5.1 | Recovery secret not sent in clear text | Accepted | Staff invite links are initial activation secrets and are sent by email, the only channel the app has. Each link is single use, expires after 72 hours, is stored only as a keyed hash (derived invite key, ADR 0009), and sending a new invite makes every older link stop working (`identity/service.py` `invite_again`). Password reset links are sent by email too: single use, 30 minutes, keyed hash only (derived reset link key), the token in the URL fragment so it reaches no server, and a new link cancels older ones (`identity/recovery.py`, ADR 0010) |
| 2.5.2 | No password hints or knowledge questions | Pass | None |
| 2.5.3 | Recovery does not reveal the password | Pass | The reset link lets the person choose a new password; the old one is never sent or shown (ADR 0010) |
| 2.5.4 | No shared or default accounts | Pass | Every account is personal; the demo prints per-person logins |
| 2.5.6 | Forgotten password uses a secure recovery | Pass | Staff ask for a reset link by email; setting the new password also needs a current authenticator code when the account has one, checked with the single-use step and attempt limit, so the email alone is not enough. The request gives the same answer and timing for every email; every session is signed out after a reset (`identity/recovery.py`, ADR 0010). A lost authenticator still goes to the clinic admin |
| 2.7.1 | Clear-text SMS not used for secrets | Pass | No SMS (out of scope in SPEC 1.2) |
| 2.7.2 | Out-of-band codes expire after 10 minutes | Pass | Email codes, 10 minutes |
| 2.7.3 | Out-of-band codes used once | Pass | The code row is read with `SELECT ... FOR UPDATE` and `used_at` is set in the same transaction (`identity/service.py` `verify_email_code`). A second request with the same code, on any worker, waits for the lock and then finds no unused code. Tested with two requests at once: `tests/identity/test_single_use.py` |
| 2.7.4 | Out-of-band channel is secure | Pass | Brevo API over HTTPS |
| 2.8.1 | Time-based OTPs have a defined lifetime | Pass | 30 second TOTP steps (the current step or one either side). Each step is accepted once per user, across all API workers and restarts: the last accepted step is kept in `mfa_secrets.last_step` (migration 0006), and a code is accepted only if one `UPDATE ... WHERE last_step IS NULL OR last_step < :step` changes the row (`identity/service.py` `_claim_step`). A step at or before the last accepted one is refused. Tested with two requests at once, after a restart, and with an older step after a newer one: `tests/identity/test_single_use.py` |

## V3 Session management

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 3.1.1 | No session tokens in URLs | Pass | Bearer header; the WebSocket sends the token in the first message, not the URL |
| 3.2.1 | New token at authentication | Pass | New session family per sign-in |
| 3.2.2 | At least 64 bits of entropy | Pass | 32-byte refresh tokens, signed JWTs |
| 3.2.3 | Tokens stored securely in the browser | Pass | Refresh token in an HttpOnly cookie; access token in memory only |
| 3.3.1 | Logout and expiry invalidate the session | Pass | Logout revokes the session; every request checks the session is still active (`identity/deps.py`) |
| 3.3.2 | Re-authentication at least every 30 days | Pass | Refresh tokens last 14 days |
| 3.4.1 | Cookies have Secure | Pass | `COOKIE_SECURE=true` everywhere, including local |
| 3.4.2 | Cookies have HttpOnly | Pass | `identity/router.py` |
| 3.4.3 | Cookies have SameSite | Pass | `SameSite=Lax` |
| 3.4.4 | `__Host-` cookie prefix | Accepted | In production the web app and the API are on two subdomains that share the refresh cookie (SPEC 2), which the `__Host-` prefix does not allow. The cookie is limited to the refresh path |
| 3.4.5 | Cookie path set | Pass | Refresh path only |
| 3.7.1 | Re-authentication before sensitive changes | Pass | Password change needs the current password; TOTP set-up needs a fresh sign-in |

## V4 Access control

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 4.1.1 | Enforced on a trusted server | Pass | Policy module and Postgres row level security |
| 4.1.2 | Attributes cannot be changed by users | Pass | Role and clinic come from the signed token and the membership table |
| 4.1.3 | Least privilege | Pass | Task views per role (SPEC 4), `access/policy.yaml` |
| 4.1.5 | Fails securely | Pass | Denials raise and are logged in their own transaction |
| 4.2.1 | Protection against direct object reference | Pass | RLS per clinic (`tests/db/test_rls.py`), patient-self rule, role tests per endpoint |
| 4.2.2 | Anti-CSRF | Pass | Bearer tokens for every API call; the cookie-based refresh is `SameSite=Lax` with a strict CORS allow list |
| 4.3.1 | Admin interfaces use MFA | Pass | Clinic admins are staff and need TOTP |
| 4.3.2 | No directory browsing or metadata files | Pass | Caddy serves only the built files; no browse |

## V5 Validation, sanitization and encoding

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 5.1.1 | HTTP parameter pollution | Pass | FastAPI typed parameters |
| 5.1.2 | Mass assignment | Pass | Explicit Pydantic input schemas |
| 5.1.3 | Input validation | Pass | Pydantic schemas with lengths, patterns and enums |
| 5.1.4 | Structured data validated | Pass | Same |
| 5.1.5 | Redirects to allowed destinations only | N/A | The API does not redirect |
| 5.2.1 | HTML input sanitized | Pass | No HTML input; React escapes output |
| 5.2.2 | Unstructured data sanitized | Pass | Text is stored and shown as text |
| 5.2.3 | Mail injection | Pass | Email through the Brevo JSON API |
| 5.2.4 | No eval or dynamic code | Pass | None |
| 5.2.5 | Template injection | N/A | No server-side templates |
| 5.2.6 | SSRF | Pass | The only user-supplied URL (web push endpoint) must be a known browser push service (`notify/router.py`) |
| 5.2.7 | SVG sanitized | Pass | SVG uploads are refused |
| 5.2.8 | Script and expression injection | Pass | None used |
| 5.3.1 | Output encoding for the context | Pass | React and JSON responses |
| 5.3.2 | Unicode-safe encoding | Pass | UTF-8 JSON |
| 5.3.3 | XSS protection | Pass | React escaping and a CSP without inline scripts or styles |
| 5.3.4 | SQL injection | Pass | SQLAlchemy bound parameters; the only formatted SQL uses a fixed allow list of role names |
| 5.3.5 | Injection in other interpreters | Pass | None |
| 5.3.6 | JSON injection | Pass | Serialised by the framework |
| 5.3.7 | LDAP injection | N/A | No LDAP |
| 5.3.8 | OS command injection | Pass | Only fixed `git` arguments in the local witness, no shell |
| 5.3.9 | File inclusion | Pass | Storage paths are generated by the server |
| 5.3.10 | XPath injection | N/A | No XML |
| 5.5.1 to 5.5.3 | Unsafe deserialization | Pass | JSON only |

## V6 Stored cryptography

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 6.2.1 | Crypto modules fail securely | Pass | AES-GCM tag failures raise; `tests/identity/test_crypto.py` |
| 6.2.2 | Industry proven or government approved algorithms, modes and libraries are used, instead of custom coded cryptography | Pass | AES-256-GCM, HMAC-SHA256, HKDF-SHA256 (RFC 5869), Ed25519 and Argon2id, all from `cryptography`, `hmac` and `argon2-cffi`. Each use of `OTP_HMAC_KEY` has its own HKDF-derived key (`core/keys.py`, ADR 0009); the HKDF call is checked against RFC 5869 test case 1 in `tests/test_keys.py` |

### V6.4 Secret management

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 6.4.1 | A secrets management solution such as a key vault is used to securely create, store, control access to and destroy secrets | Accepted | No vault: secrets live in `/opt/healthsaathi/.env` (chmod 600) on the one VM and in GitHub Actions secrets. The deployment is one free-tier VM and a paid or extra key service is out of scope (CLAUDE.md, zero cost). Secrets are never in the repo (`.env.example` holds names only), and `docs/RUNBOOK.md` lists how to rotate each one |
| 6.4.2 | Key material is not exposed to the application but uses an isolated security module like a vault for cryptographic operations | Accepted | The API process holds the keys in memory for the same reason as 6.4.1. Keys are never logged or returned by an endpoint (`tests/test_keys.py`), and one derived key per use limits what a single misuse exposes (ADR 0009) |

## V7 Error handling and logging

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 7.1.1 | No credentials or session tokens in logs | Pass | Email bodies (with codes) are logged only when `APP_ENV=local` |
| 7.1.2 | No other sensitive data in logs | Pass | Request logs hold method, path, status and request id |
| 7.4.1 | Generic error message with an id | Pass | RFC 7807 responses with `request_id`; the UI shows translated messages only |

## V8 Data protection

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 8.2.1 | Anti-caching headers on sensitive data | Pass | `Cache-Control: no-store` on every API response |
| 8.2.2 | Little sensitive data in browser storage | Pass | Local storage holds language and the chosen clinic; vitals drafts are encrypted with an in-memory key |
| 8.2.3 | Browser data cleared after logout | Pass | Caches and drafts are dropped on sign-out |
| 8.3.1 | No sensitive data in query strings | Accepted | Patient search sends the typed name or phone as `?q=` to a GET endpoint. The API's own logs record the path without the query; the reverse proxy log line keeps it, on the clinic's own server. Changing the search to POST is a possible later change |
| 8.3.2 | Users can export and delete their data | Accepted | Export: JSON and FHIR download. Deletion: withdrawing consent stops further use; clinical records are kept as the law requires, which the consent notice states |
| 8.3.3 | Clear language about data collection | Pass | Consent notice in English and Hindi at registration |
| 8.3.4 | Sensitive data identified and handled | Pass | SPEC 3 and 4; notes and TOTP secrets encrypted |

## V9 Communication

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 9.1.1 | TLS for all client connections | Accepted for the local demo | Production: Caddy with automatic HTTPS for the API and Cloudflare Pages for the app, HSTS on both. The local demo runs on http://localhost, which browsers treat as a secure context |
| 9.1.2 | Strong cipher suites | Pass | Caddy and Cloudflare defaults |
| 9.1.3 | Old TLS versions disabled | Pass | TLS 1.2 and later (Caddy default) |

## V10 Malicious code

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 10.3.1 | Updates are integrity protected | Pass | The service worker and assets come from the app's own origin over HTTPS |
| 10.3.2 | Subresource integrity for third-party code | Pass | The app loads no third-party scripts; fonts are bundled |

## V11 Business logic

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 11.1.1 | Steps in order | Pass | Status transitions are checked (appointments, queue, lab orders, notes) |
| 11.1.2 | Realistic time for steps | Pass | Sign-in limits counted in the database (see 2.2.1); search, export and print limits per user and route, and the default 120 per minute per user or IP, counted per worker |
| 11.1.3 | Business limits enforced | Pass | Overpayment, signed records immutable, break-glass window |
| 11.1.4 | Anti-automation for business functions | Pass | Rate limits (export and print 10 per minute) |
| 11.1.5 | Limits on business logic attacks | Pass | Break-glass review, alert budget, own-review refused |

## V12 Files and resources

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 12.1.1 | Large files refused | Pass | 10 MB limit (`MAX_UPLOAD_BYTES`) |
| 12.3.1 | User file names not used in paths | Pass | Paths are generated |
| 12.3.2 | No path traversal in file metadata | Pass | Same |
| 12.3.3 | No remote file inclusion | Pass | None |
| 12.3.4 | No reflected file download | Pass | Downloads are attachments with a fixed type |
| 12.3.5 | No command execution from file metadata | Pass | None |
| 12.4.1 | Files stored outside the web root | Pass | `STORAGE_DIR` or Azure Blob |
| 12.4.2 | Files scanned for malware | Accepted | Only PDF, PNG and JPEG with matching signatures are accepted, and files are never executed or rendered by the server. No free scanner fits the zero-cost hosting |
| 12.5.1 | Only allowed file types served | Pass | Caddy serves the build only |
| 12.5.2 | Uploaded files not executed as HTML | Pass | `Content-Disposition: attachment` and `nosniff` |
| 12.6.1 | Server-side requests to allowed resources only | Pass | See 5.2.6 |

## V13 API

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 13.1.1 | Same encoding and parsers everywhere | Pass | JSON only |
| 13.1.3 | No sensitive data in API URLs | Pass | No keys or tokens in URLs (see 8.3.1 for search) |
| 13.2.1 | Only allowed HTTP methods | Pass | Unknown methods return 405 |
| 13.2.2 | JSON schema validation | Pass | Pydantic, OpenAPI published |
| 13.2.3 | CSRF protection for cookie-based APIs | Pass | See 4.2.2 |

## V14 Configuration

| ID | Requirement (short) | Status | Evidence |
|---|---|---|---|
| 14.2.1 | Components up to date and checked | Pass | `make audit` (pip-audit and pnpm audit) in CI |
| 14.2.2 | Unneeded features off | Pass | API docs only when `APP_ENV` is local or test; dev mailbox only when local |
| 14.2.3 | Third-party assets with integrity | Pass | None loaded |
| 14.3.2 | Debug modes off in production | Pass | No debug mode; unhandled errors return `internal-error` |
| 14.3.3 | No version details in headers | Pass | `Server` header removed by Caddy |
| 14.4.1 | Content type with charset | Pass | JSON responses |
| 14.4.2 | API downloads are attachments | Pass | Document downloads |
| 14.4.3 | Content Security Policy | Pass | `default-src 'self'`, no inline scripts or styles, `frame-ancestors 'none'` (web); `default-src 'none'` (API) |
| 14.4.4 | `X-Content-Type-Options: nosniff` | Pass | Web and API |
| 14.4.5 | HSTS | Pass | API and hosted web app |
| 14.4.6 | Referrer-Policy | Pass | Web and API |
| 14.4.7 | Not framable | Pass | `frame-ancestors 'none'` and `X-Frame-Options: DENY` |
| 14.5.1 | Only needed HTTP methods | Pass | See 13.2.1 |
| 14.5.2 | Origin header not used for access decisions | Pass | Only for CORS |
| 14.5.3 | CORS allow list | Pass | `CORS_ORIGINS` |

## 15. Scans of the local stack

| Scan | How | Result |
|---|---|---|
| OWASP ZAP baseline, web app | `zap-baseline.py -t http://host.docker.internal:8080` (ZAP stable image) against `make demo` | 0 failures. Remaining warnings: Cross-Origin-Embedder-Policy not set (accepted: the app uses nothing that needs cross-origin isolation, such as SharedArrayBuffer); "storable and cacheable content" on the hashed build assets (intended, they are immutable); "modern web application" (informational) |
| OWASP ZAP baseline, API | `zap-baseline.py -t http://host.docker.internal:8081/api/v1/health` | 0 failures. "Non-storable content" warnings are the intended `no-store` |
| axe-core, WCAG 2.0 and 2.1 A and AA | `apps/web/e2e-demo/a11y.spec.ts`, every role's screens and tabs, English and Hindi | 0 violations |
| Browser crawler | `apps/web/e2e-demo/crawl.spec.ts`, every role and route at desktop width, 360 px and in Hindi | No console errors, no 4xx or 5xx responses, no untranslated keys, no stuck loading states |

Re-run: `make demo`, then `make demo-e2e`; ZAP with the commands above.

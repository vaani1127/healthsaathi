# Full app audit

Branch `chore/full-app-audit`, 20 November 2026. Goal: prove that what exists works and record
every real problem. Nothing is fixed in this step. Fixes follow in small commits, one problem per
commit, each with a test.

## Severity

Defined before the walkthrough:

- **Blocker**: wrong or lost data, a security or privacy hole, wrong patient data shown, or a main
  flow (SPEC 1.3 flows 1 to 9) cannot be finished.
- **Major**: a feature does not work, or works only with steps a normal user would not find.
- **Minor**: works, but with a wrong message, missing translation, layout break or a confusing
  step.
- **Cosmetic**: looks wrong, works right.

## Summary

| Severity | Count |
|---|---|
| Blocker | 0 |
| Major | 1 |
| Minor | 5 |
| Cosmetic | 1 |

Every main flow (1 to 9) finishes: `make e2e` 12 passed and `make demo-e2e` 56 passed, both on the
final gate below. The walkthrough found no console error, failed request, untranslated text, raw
error text, page wider than the screen, stuck loading line or accessibility (axe WCAG 2.0/2.1 A
and AA) violation on any screen it reached, in English or Hindi, at desktop or 360 px, apart from
the items listed under Problems.

## How it was checked

| Part | Tool | Output |
|---|---|---|
| Inventory | `infra/audit/inventory.py` | Section "Inventory" below |
| Routes called by the API tests | `HS_ROUTE_LOG` hook in `apps/api/tests/conftest.py` (test code only) | `audit-artifacts/routes-pytest.json` |
| Routes called by `make e2e` | the API's request log, shown when `HS_ROUTE_LOG` is set (`apps/web/playwright.config.ts`) | `audit-artifacts/g-e2e.log` |
| Routes called by `make demo-e2e` and `make demo-audit` | `docker compose logs -f api`, followed live and reattached when the container is recreated | `audit-artifacts/demo-e2e-api.log`, `audit-artifacts/audit-api.log` |
| Walkthrough | `make demo-audit` (`apps/web/e2e-demo/audit/walkthrough.spec.ts`), demo running with rate limits on | `audit-artifacts/walkthrough/*.json`, screenshots under `audit-artifacts/` |
| PWA cache check | the same spec, tests "pwa cache check clinic a doctor/patient" | `audit-artifacts/walkthrough/pwa-*.json` |
| Docs check | `infra/audit/docs_check.py` plus reading | Section "Docs truth check" |
| Fresh clone | the README development steps that need no Docker or database, in a new folder outside the workspace (deleted afterwards) | Section "Fresh clone check" |

The URLs of the browser runs come from the API's own request log (method and path of every
request the browser sent), mapped to route templates, not from listeners in Playwright: the specs
share no fixture where a listener could be added without changing every spec. WebSocket
connections are not in that log, so `/api/v1/ws` shows as called by the API tests only.

`audit-artifacts/` is gitignored. Screenshot paths below are relative to the repository root.

## Problems

### Major

**M1. The README development steps give an API that does not start.**

- Screen: none (developer setup, README "Development setup").
- Steps: in a fresh clone run the README steps `cp .env.example .env`, `make install`,
  `make dev`.
- Expected: Postgres, the API on :8000 and the web app on :5173.
- Actual: `.env.example` leaves `JWT_SECRET`, `OTP_HMAC_KEY` and `LEDGER_SIGNING_SEED` empty.
  `make dev` first runs `make env`, which does nothing because `.env` already exists. The API's
  settings then refuse to load: `ValidationError: 3 validation errors for Settings ... Value
  error, must be at least 32 characters` (reproduced by building `Settings` with those three
  values empty; `make dev` itself was not run, because the fresh clone check excluded Docker).
  `make env` without the `cp` step writes fresh secrets and is what works.
- Screenshot: none (command output above).

### Minor

**m1. An unknown address shows a bare "Not Found", in English only, with no way back.**

- Screen: any unknown path, for example `/no-such-page`.
- Steps: open `http://localhost:8080/no-such-page`, in English or Hindi.
- Expected: a translated "page not found" message inside the app, with a link home.
- Actual: the router's default text "Not Found" on an empty page, in English in the Hindi run,
  no header and no link.
- Screenshot: `audit-artifacts/a-public/hi-360/09-no-such-page.png` (also `en-*`).

**m2. Saving an empty vitals form gives a generic message.**

- Screen: nurse, patient page, "Record vitals".
- Steps: open a patient as nurse, press "Save vitals" with every field empty.
- Expected: a message that at least one value is needed, or the field to fix marked.
- Actual: the request is sent and refused (422), and the page shows "Some fields are not valid.
  Check them and try again." with no field marked, although every field is empty.
- Screenshot: `audit-artifacts/b-nurse/en-desktop/click-004-nurse-patients-id-Save-vitals.png`.

**m3. After sign-out, the browser keeps the address of the cached queue.**

- Screen: any staff screen, then "Sign out".
- Steps: sign in as a doctor, open the queue (cached for offline use), sign out, list IndexedDB.
- Expected: nothing of the signed-out user left in browser storage.
- Actual: the API cache `hs-api-queue` is deleted as designed, but Workbox's own bookkeeping
  database `workbox-expiration` keeps one row,
  `hs-api-queue|http://localhost:8080/api/v1/queue?doctor_user_id=<doctor's user id>`. No
  patient data, only the queue address with the doctor's own user id.
- Evidence: `audit-artifacts/walkthrough/pwa-doctor.json` (`workboxMetadataAfterSignOut`).

**m4. Admin actions that change staff, schedules and services take effect on one click.**

- Screen: clinic admin, tabs Staff, Schedules, Services and Emergency access.
- Steps: press "Deactivate" on a staff member, "Pause" on a schedule or service, or a review
  button on a break-glass item.
- Expected: a confirmation, or an undo message, for changes that stop a person signing in or a
  doctor taking bookings.
- Actual: each acts at once. In the audit one pass over the clinic B admin screen deactivated 8
  staff members and paused 20 schedules and 5 services (see "Clinic B data changes"). Each can
  be undone ("Activate", "Resume"); a break-glass review cannot be changed.
- Screenshots: `audit-artifacts/b-clinic_admin/en-desktop/click-041-admin-Staff-Deactivate.png`
  and `audit-artifacts/b-clinic_admin/en-desktop/click-050-admin-Schedules-Pause.png` (one per click).

**m5. The user guide does not mention sending an invite again.**

- Screen: docs, `docs/USER_GUIDE.md`, "Clinic admin" > "Staff".
- Expected: the Staff tab as it is: "No password yet" and "Send invite again" (added on
  `fix/invite-resend`).
- Actual: the section only mentions inviting, deactivating and "No authenticator yet".

### Cosmetic

**c1. Opening a patient without a reason logs a console error.**

- Screen: doctor, a patient who is not in the doctor's queue.
- Steps: open `/doctor/patients/<id>` for such a patient.
- Expected and actual: the "Reason needed" prompt appears, as designed. The chart request is
  answered 428 (the designed "reason required" answer), and the browser logs it as "Failed to
  load resource: the server responded with a status of 428".
- Screenshot: `audit-artifacts/a-doctor/en-360/03-doctor-patients-id.png`.

### Found and fixed in the audit tooling, not in the app

- The first walkthrough run's patient tests all signed in as one patient, and the 4th code
  request in a minute is answered but sends nothing (the per-address limit, by design). The
  sign-in helper did not notice and recorded the sign-in page as every patient screen. Each
  patient test now uses its own patient login and requires the portal (commit `27a3f92`).
- A patient reloading the page keeps the session (checked: `POST /auth/refresh` 200, stays on
  `/patient`); that was not an app problem.
- Docker Desktop stopped during the first walkthrough run (exit code 4 after test 32). The WSL
  memory was set to 6 GB (`C:\Users\DELL\.wslconfig`, with the user's approval) and the runs
  below finished.

## Cross-checks

| Check | Result | Evidence |
|---|---|---|
| PWA: notes, labs and prescriptions never cached (SPEC 4) | Pass, doctor and patient | "PWA cache check" below |
| PWA: sign-out clears cached API responses and drafts | Pass, with m3 | "PWA cache check" below |
| Every 4xx and 5xx has an English and Hindi message | Pass: every code the server raises is translated (`apps/api/tests/test_error_translations.py`); status fallbacks exist for 400, 401, 403, 404, 409, 413, 415, 422, 429 and 500, and every 5xx uses the 500 one. 405 and 428 have no status fallback: 428 always carries its own translated code, and the UI never sends a 405 request | `apps/web/src/locales/*.json`, `apps/web/src/lib/format.ts` |
| No console errors | Pass on every reached screen, apart from the browser's own log of the 428 and 422 answers (c1, m2) | walkthrough JSON |
| Accessibility (axe, WCAG 2.0/2.1 A and AA) | 0 violations on every reached screen, both languages, both sizes | walkthrough JSON |
| Break-glass end to end | Pass (flow 7 in `make e2e` and `make demo-e2e`) | final gate |
| "I do not recognise this access" end to end | Pass (flow 6, patient report; the admin's "Patient reports" tab reached in the walkthrough) | final gate, walkthrough |
| Receipt verification | Pass (demo flow 9 receipt check; `apps/web/test/verify.test.tsx` shows a tampered receipt as not verified) | final gate |
| Audit page checks | Pass (flow 9 chain, consistency and witness; `e2e-demo/tamper.spec.ts` shows FAIL after a rewrite) | final gate |
| FHIR export | Not run (`make fhir-validate` needs the HAPI validator image); see Not verified | |
| Daily job | Pass in the API tests (`tests/detect/test_daily*.py`); the alert day view reached in flow 8 | final gate |
| Invite and resend | Pass (`e2e/staff-invite.spec.ts` in both suites) | final gate |
| Rate limit messages | Server side and the web 429 handling pass (`tests/test_default_limit.py`, `tests/identity/test_db_limits.py`, `apps/web/test/rateLimited.test.tsx`); not triggered in a browser in this audit | |

## PWA cache check

Production build of the demo, service worker in control (`controlled: true`), after one reload
so the runtime caching applies.

**Clinic A doctor (`doctor1.a`): pass.**

- Opened from the doctor's queue: two patient charts, each chart's lab report print and
  prescription print (6 pages).
- Fetched: 2 `GET /api/v1/patients/<id>/chart`, 2 `GET /api/v1/lab-orders/<id>/print`, 2
  `GET /api/v1/prescriptions/<id>/print`.
- Before sign-out, Cache Storage: `workbox-precache-v2-http://localhost:8080/` (62 app files, no
  API responses), `hs-config` (`/config.js`), `hs-api-queue`
  (`/api/v1/queue?doctor_user_id=01958152-2840-7b23-a5ea-77cb7c97e937`). IndexedDB:
  `workbox-expiration` / `cache-entries` (the queue entry).
- After sign-out, Cache Storage: the precache (62 app files) and `hs-config` only;
  `hs-api-queue` deleted. IndexedDB: `hs-drafts` / `vitals` empty; `workbox-expiration` still
  has the queue entry (m3).
- No notes, lab or prescription response in any cache or store.

**Clinic A patient (`patient1.a`): pass.**

- Opened: `/patient` and `/patient/print`. Fetched: `GET /api/v1/patients/<id>/chart`.
- Before sign-out: the precache (62 app files) and `hs-config` (`/config.js`); no IndexedDB
  store.
- After sign-out: the same two caches; IndexedDB `hs-drafts` / `vitals` empty.
- No patient record response in any cache or store.

## Untested

**API routes never called in any run**: 1 of 114, `GET /api/v1/openapi.json`.
`tests/test_openapi_export.py` builds the same document directly, so it is covered, just not over
HTTP.

**Worker and startup jobs without a direct test**: 6, the loops `detect_loop`, `push_loop`,
`prune_loop`, `worker.main`, `maintenance_loop` and `run_startup_tasks`. The work each loop does
(`detect_tick`, `push.send_pending`, `prune_counters_tick`, `ensure_partitions`,
`register_policy_version`, the anchor timer) is tested; the loops run in the demo, where
detection and anchoring visibly work (flows 8 and 9), but no test checks the loops themselves.

**API routes that no browser run called** (API tests only): 33.

`PATCH /access-queries/{query_id}`, `PATCH /allergies/{allergy_id}`, `POST /auth/password`,
`DELETE /auth/sessions/{session_id}`, `POST /auth/totp/activate`, `POST /care-team`,
`POST /care-team/{assignment_id}/end`, `PATCH /conditions/{condition_id}`,
`POST /consent-notices`, `POST /consent-notices/{notice_id}/publish`,
`POST /consents/{consent_id}/withdraw`, `GET /documents/{document_id}`,
`POST /encounters/{encounter_id}/close`, `POST /internal/anchor/run`, `GET /invoices`,
`POST /invoices/{invoice_id}/void`, `PATCH /notes/{note_id}`, `GET /notes/{note_id}/history`,
`POST /notes/{note_id}/versions`, `PATCH /patients/{patient_id}`,
`POST /patients/{patient_id}/allergies`, `POST /patients/{patient_id}/conditions`,
`GET /patients/{patient_id}/emergency` (used only when a doctor reloads the page during an open
break-glass window), `POST /patients/{patient_id}/referrals`, `PATCH /prescriptions/{rx_id}`,
`POST /prescriptions/{rx_id}/versions`, `POST /push/subscriptions`,
`POST /push/subscriptions/remove`, `POST /schedules`, `POST /services`, `POST /shifts`,
`POST /shifts/{shift_id}/cancel`, `WS /ws` (used in the browser; WebSocket connections are not in
the request log). All paths are under `/api/v1`.

**Screens the walkthrough did not record**: `/`, `/login/totp`, `/login/enrol` and
`/select-clinic`. The first three are passed through on every staff sign-in, and
`e2e/staff-invite.spec.ts` reaches the enrolment page, but the walkthrough did not record them as
screens.

**Walkthrough depth**: no test reached the 45-screen limit (most: 12). The clinic A doctor
reached only 4 screens: the patients found through the reception search open on "Reason needed",
and they used up the two visits allowed per address pattern before the doctor's queue links.
Doctor charts were opened by the PWA check, the clinic B doctor run and the flows instead.

## Docs truth check

- Every quoted UI name in `docs/USER_GUIDE.md` and `docs/DEMO_SCRIPT.md` is a text the app shows
  (`infra/audit/docs_check.py`); the rest of those flags are examples, not UI names.
- Every make target named in the README, USER_GUIDE, DEMO_SCRIPT and RUNBOOK exists. The one
  flag, `docs/RUNBOOK.md:25` "prod-", is the start of `make prod-<step>` text.
- Every file and test cited in `docs/SECURITY_CHECKLIST.md` exists (the two flags are ZAP
  commands, not files).
- Behaviour spot checks that match the code: the QR code on first sign-in (`EnrolPage.tsx`),
  "Walk-in", the 4 hour break-glass window (`access/service.py` `BREAK_GLASS_WINDOW`), lab report
  files PDF, PNG or JPEG (`clinical/service.py` `ALLOWED_UPLOADS`), choosing a receipt file on
  `/verify`, the insider-case table printed by `make demo`.

Mismatches:

| Doc | What it says | What is true |
|---|---|---|
| `README.md`, "Development setup" | `cp .env.example .env`, then `make dev` | The API does not start with the empty secrets; `make env` is the step that works (M1) |
| `docs/USER_GUIDE.md:107-108` | Staff: invite, deactivate, "No authenticator yet" | Also "No password yet" and "Send invite again" (m5) |
| `apps/web/vite.config.ts` comment | "SPEC 4: today's queue is available offline" | Matches SPEC 4; the audit request named SPEC 9, which is the frontend structure. Not a mismatch, noted so it is not looked for again |

Not every sentence of the RUNBOOK was checked against a run: its production steps (Azure,
Cloudflare, GitHub secrets, deploy, rollback, rotation) were read against the scripts, not run.

## Fresh clone check

`git clone --recurse-submodules` of this repository (committed state of this branch) into a new
folder outside the workspace, then the README development steps that need no Docker or database:

| Step | Result |
|---|---|
| `git clone --recurse-submodules` | exit 0; `contracts/lib/forge-std` checked out |
| `cp .env.example .env` | exit 0 (see M1) |
| `make install` | exit 0 |
| `make help` | exit 0 |
| `make lint` | exit 0 |
| `cd apps/web && pnpm build` | exit 0; PWA precache 66 entries |

The README clones from GitHub; this check cloned the local repository, so the GitHub address
itself was not checked. The folder was deleted afterwards with `rm -rf` on its full scratchpad
path.

## Final gate

Run after the walkthrough, with the demo stopped for `make test`:

| Step | Result |
|---|---|
| `make lint` | exit 0: ruff all checks passed, 311 files formatted; mypy no issues in 280 files; contracts 3 kept, 0 broken; eslint, tsc, forge fmt clean |
| `make test` | exit 0: pytest 1006 passed in 434.4 s, coverage 99.71%; JS 63 and 27 passed; Solidity 30 and 21 passed |
| `make e2e` | exit 0: 12 passed, 2 passed |
| `make demo` | exit 0 |
| `make demo-e2e` | exit 0: 56 passed |

`make demo-e2e` also passed (56) right after the walkthrough, before the gate: the clinic B changes
broke no demo test.

## Not verified

- `make dev` was not run (Docker excluded from the fresh clone check); M1 was reproduced by
  building the API settings with the empty secrets.
- `make fhir-validate` (FHIR export against the HAPI validator) was not run.
- The rate limit messages were not triggered in a browser; the server tests and the web unit test
  cover them.
- The 33 routes listed under "Untested" were not exercised in a browser, so their screens' error
  states were not seen.
- The RUNBOOK's production steps were read, not run. Nothing was run on the prod VM.
- The Hindi screens were checked for Hindi text and untranslated keys, not read by a Hindi
  speaker.
- The walkthrough clicked buttons with empty forms in clinic B; positive paths of each form were
  covered by the flows in clinic A, not by the walkthrough.
- The README's GitHub clone address was not used (the local repository was cloned).

## Clinic B data changes

Everything the walkthrough changed in clinic B, so it can be reset before a final demo. 53
requests are logged; 51 changed data (the 2 vitals saves answered 422 changed nothing). Clinic B's
admin account was never deactivated or locked. **8 of the 9 clinic B staff memberships are now
inactive**, so those staff cannot sign in until an admin activates them again or the demo is
reset.

Summary: setup day 10 changes (two appointments, one token, one walk-in, vitals, an encounter, a
draft note, a prescription, a CBC order, sample collected, for Imran Mishra and Jaya Khan); admin
8 staff deactivated, 20 schedules paused, 5 services paused, 4 break-glass items reviewed
"Justified"; doctor signed the setup note and prescription and cancelled one referral; reception
marked Jaya Khan "No-show".

Every logged request (`audit-artifacts/clinic-b-changes.json`):

| # | How | Account | Action | Record | Request |
|---|---|---|---|---|---|
| 1 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Dr. Bhavna ChauhanDoctor - doctor1.b@demo.healthsaathi.testD | `PATCH /api/v1/staff/members/018f5e9e-8c40-7b57-9bc5-6cd12dc0689d -> 204` |
| 2 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Dr. Gurpreet ThakurDoctor - doctor2.b@demo.healthsaathi.test | `PATCH /api/v1/staff/members/0190bcce-fc40-7c26-8ed0-397691358c7d -> 204` |
| 3 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Dr. Imran SaxenaDoctor - doctor3.b@demo.healthsaathi.testDea | `PATCH /api/v1/staff/members/01948f47-4440-7c40-a3d1-f7e137d1558b -> 204` |
| 4 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Dinesh VermaLab technician - lab.b@demo.healthsaathi.testDea | `PATCH /api/v1/staff/members/01933116-d440-7bd7-84d2-a4d220bbe6b7 -> 204` |
| 5 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Harish KapoorNurse - nurse2.b@demo.healthsaathi.testDeactiva | `PATCH /api/v1/staff/members/0193efa2-2040-7560-8f96-6a6050153fdd -> 204` |
| 6 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Rajesh BansalNurse - nurse1.b@demo.healthsaathi.testDeactiva | `PATCH /api/v1/staff/members/01931757-0840-7d9f-a08e-23de604d20af -> 204` |
| 7 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Manisha BhatiaReception - reception2.b@demo.healthsaathi.tes | `PATCH /api/v1/staff/members/018f8cf7-c840-7455-965f-0f51e8efdffe -> 204` |
| 8 | click | admin.b@demo.healthsaathi.test | /admin#Staff > Deactivate | Shalini ShahReception - reception1.b@demo.healthsaathi.testD | `PATCH /api/v1/staff/members/018f2b1e-f440-7a16-b205-666fa2bd07ac -> 204` |
| 9 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7db3-923c-3ffca243ccfd -> 200` |
| 10 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7db8-8b77-87f82b027d03 -> 200` |
| 11 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7674-81d0-fd7f5bb703b7 -> 200` |
| 12 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-78e7-8b7f-11551ec17572 -> 200` |
| 13 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7b47-9a7c-9d1bc138dc2d -> 200` |
| 14 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Monday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7411-be47-55ef1bf085fd -> 200` |
| 15 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-76f3-8ec3-9e0585c7373f -> 200` |
| 16 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-718b-83ef-181cd0275f57 -> 200` |
| 17 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-72bd-b3ae-c7babde3ff25 -> 200` |
| 18 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7859-828e-688298021d5d -> 200` |
| 19 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-75b5-8619-7d4dc0bdbc0c -> 200` |
| 20 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Tuesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7901-b5b6-f92242897fc7 -> 200` |
| 21 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-71a7-be2a-3b41fd826d1d -> 200` |
| 22 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7a09-b2f2-1686f24b7e36 -> 200` |
| 23 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-70f2-9e61-25d3507a6636 -> 200` |
| 24 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7fbf-9096-6a227e75d1ae -> 200` |
| 25 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7981-9b1a-c3a318667932 -> 200` |
| 26 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Wednesday 17:00–20:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-7a9a-ac4d-46fd51e41296 -> 200` |
| 27 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Thursday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-70d0-9e02-18daeaf2b7bf -> 200` |
| 28 | click | admin.b@demo.healthsaathi.test | /admin#Schedules > Pause | Thursday 09:00–13:30 - Pause | `PATCH /api/v1/schedules/018eb9d3-0c40-79f0-a858-6777d2f96e88 -> 200` |
| 29 | click | admin.b@demo.healthsaathi.test | /admin#Services > Pause | Consultation - ₹300.00Pause | `PATCH /api/v1/services/018eb9d3-0c40-7393-823a-8eabbe24142b -> 200` |
| 30 | click | admin.b@demo.healthsaathi.test | /admin#Services > Pause | Dressing - ₹150.00Pause | `PATCH /api/v1/services/018eb9d3-0c40-72c4-8671-9a93d5ab80e9 -> 200` |
| 31 | click | admin.b@demo.healthsaathi.test | /admin#Services > Pause | ECG - ₹250.00Pause | `PATCH /api/v1/services/018eb9d3-0c40-7567-8f1d-9ea4a00d25a4 -> 200` |
| 32 | click | admin.b@demo.healthsaathi.test | /admin#Services > Pause | Follow-up consultation - ₹200.00Pause | `PATCH /api/v1/services/018eb9d3-0c40-7542-9593-a752efa94e90 -> 200` |
| 33 | click | admin.b@demo.healthsaathi.test | /admin#Services > Pause | Injection - ₹100.00Pause | `PATCH /api/v1/services/018eb9d3-0c40-7a25-8023-7d5b0d378c35 -> 200` |
| 34 | click | admin.b@demo.healthsaathi.test | /admin#Emergency access > Justified | Dr. Imran Saxena (Doctor) - Gauri Sharma9 Nov 2026 05:08 pm  | `POST /api/v1/break-glass/019bd60c-5783-745f-806d-0be19167d509/review -> 200` |
| 35 | click | admin.b@demo.healthsaathi.test | /admin#Emergency access > Justified | Dr. Imran Saxena (Doctor) - Sarita Yadav9 Nov 2026 05:07 pm  | `POST /api/v1/break-glass/019bd60b-6d23-747f-8b94-bcd394a2a85d/review -> 200` |
| 36 | click | admin.b@demo.healthsaathi.test | /admin#Emergency access > Justified | Dr. Imran Saxena (Doctor) - Satish Chopra9 Nov 2026 05:06 pm | `POST /api/v1/break-glass/019bd60a-82c3-731b-8c47-efd467158102/review -> 200` |
| 37 | click | admin.b@demo.healthsaathi.test | /admin#Emergency access > Justified | Dr. Imran Saxena (Doctor) - Sunita Patel31 Oct 2026 02:10 pm | `POST /api/v1/break-glass/019ba710-4a6f-7270-9eb4-4d2ffbab42d4/review -> 200` |
| 38 | click | doctor1.b@demo.healthsaathi.test | /doctor/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf > Sign note | 20 Nov 2026 10:56 pm - v1 DraftAudit setup: cough for 3 days | `POST /api/v1/notes/01a1f9ff-0876-7737-a565-1e167ed130f5/sign -> 200` |
| 39 | click | doctor1.b@demo.healthsaathi.test | /doctor/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf > Sign | 20 Nov 2026 10:56 pm - v1 DraftParacetamol 500 mg - 1-0-1Sig | `POST /api/v1/prescriptions/01a1f9ff-0acd-7096-a83e-db15610f2bd2/sign -> 200` |
| 40 | click | doctor1.b@demo.healthsaathi.test | /doctor/patients/019b048c-2a1d-70a6-b500-8b4ef6255004 > Cancel referral | Dr. Gurpreet Thakur to Dr. Bhavna Chauhan ActiveSecond opini | `PATCH /api/v1/referrals/019bd687-3ae0-7d55-b0a3-696ffd365eb9 -> 200` |
| 41 | click | nurse1.b@demo.healthsaathi.test | /nurse/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf > Save vitals |  | `POST /api/v1/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf/vitals -> 422` |
| 42 | click | nurse1.b@demo.healthsaathi.test | /nurse/patients/019b048c-2a1d-70a6-b500-8b4ef6255004 > Save vitals |  | `POST /api/v1/patients/019b048c-2a1d-70a6-b500-8b4ef6255004/vitals -> 422` |
| 43 | click | reception1.b@demo.healthsaathi.test | /reception > No-show | Jaya Khan10:56 pm - Checked inToken 2No-showCancel | `PATCH /api/v1/appointments/01a1f9fe-f09e-715a-9c1b-4062da8c8421 -> 200` |
| 44 | setup | reception1.b@demo.healthsaathi.test | book today with the clinic B doctor and issue a token | Imran Mishra | `POST /api/v1/appointments -> 201` |
| 45 | setup | reception1.b@demo.healthsaathi.test | book today with the clinic B doctor and issue a token | Imran Mishra | `POST /api/v1/queue/tokens -> 201` |
| 46 | setup | reception1.b@demo.healthsaathi.test | add a walk-in for the clinic B doctor | Jaya Khan | `POST /api/v1/walkins -> 201` |
| 47 | setup | nurse1.b@demo.healthsaathi.test | take in and record vitals | Imran Mishra | `PATCH /api/v1/queue/tokens/01a1f9fe-8860-7091-95ee-e1c0871b3ed3 -> 200` |
| 48 | setup | nurse1.b@demo.healthsaathi.test | take in and record vitals | Imran Mishra | `POST /api/v1/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf/vitals -> 201` |
| 49 | setup | doctor1.b@demo.healthsaathi.test | consultation, draft note, prescription, CBC | Imran Mishra | `POST /api/v1/appointments/01a1f9fe-8217-76c3-b759-160fe9039ac6/encounter -> 201` |
| 50 | setup | doctor1.b@demo.healthsaathi.test | consultation, draft note, prescription, CBC | Imran Mishra | `POST /api/v1/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf/notes -> 201` |
| 51 | setup | doctor1.b@demo.healthsaathi.test | consultation, draft note, prescription, CBC | Imran Mishra | `POST /api/v1/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf/prescriptions -> 201` |
| 52 | setup | doctor1.b@demo.healthsaathi.test | consultation, draft note, prescription, CBC | Imran Mishra | `POST /api/v1/patients/019291bb-d44f-7d5e-8af6-78168e32f2bf/lab-orders -> 201` |
| 53 | setup | lab.b@demo.healthsaathi.test | sample collected | Imran Mishra | `PATCH /api/v1/lab-orders/01a1f9ff-0cc2-74f0-8cc9-f20e381ceef2 -> 200` |

## Walkthrough coverage

From `audit-artifacts/walkthrough/*.json`.

| Test | Account | Screens | Buttons clicked | Data changes | Hit the 45-screen cap |
|---|---|---|---|---|---|
| clinic a clinic_admin en 360 | admin.a@demo.healthsaathi.test | 12 | 0 | 0 | no |
| clinic a clinic_admin en desktop | admin.a@demo.healthsaathi.test | 12 | 0 | 0 | no |
| clinic a clinic_admin hi 360 | admin.a@demo.healthsaathi.test | 12 | 0 | 0 | no |
| clinic a clinic_admin hi desktop | admin.a@demo.healthsaathi.test | 12 | 0 | 0 | no |
| clinic a doctor en 360 | doctor1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a doctor en desktop | doctor1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a doctor hi 360 | doctor1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a doctor hi desktop | doctor1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a lab_tech en 360 | lab.a@demo.healthsaathi.test | 2 | 0 | 0 | no |
| clinic a lab_tech en desktop | lab.a@demo.healthsaathi.test | 2 | 0 | 0 | no |
| clinic a lab_tech hi 360 | lab.a@demo.healthsaathi.test | 2 | 0 | 0 | no |
| clinic a lab_tech hi desktop | lab.a@demo.healthsaathi.test | 2 | 0 | 0 | no |
| clinic a nurse en 360 | nurse1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a nurse en desktop | nurse1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a nurse hi 360 | nurse1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a nurse hi desktop | nurse1.a@demo.healthsaathi.test | 4 | 0 | 0 | no |
| clinic a patient en 360 | patient1.a@demo.healthsaathi.test | 7 | 0 | 0 | no |
| clinic a patient en desktop | patient2.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic a patient hi 360 | patient3.a@demo.healthsaathi.test | 7 | 0 | 0 | no |
| clinic a patient hi desktop | patient4.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic a public en 360 | none | 9 | 0 | 0 | no |
| clinic a public en desktop | none | 9 | 0 | 0 | no |
| clinic a public hi 360 | none | 9 | 0 | 0 | no |
| clinic a public hi desktop | none | 9 | 0 | 0 | no |
| clinic a reception en 360 | reception1.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic a reception en desktop | reception1.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic a reception hi 360 | reception1.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic a reception hi desktop | reception1.a@demo.healthsaathi.test | 8 | 0 | 0 | no |
| clinic b clinic_admin actions en desktop | admin.b@demo.healthsaathi.test | 12 | 85 | 37 | no |
| clinic b doctor actions en desktop | doctor1.b@demo.healthsaathi.test | 7 | 21 | 3 | no |
| clinic b lab_tech actions en desktop | lab.b@demo.healthsaathi.test | 2 | 4 | 0 | no |
| clinic b nurse actions en desktop | nurse1.b@demo.healthsaathi.test | 4 | 7 | 2 | no |
| clinic b patient actions en desktop | patient1.b@demo.healthsaathi.test | 8 | 25 | 0 | no |
| clinic b reception actions en desktop | reception1.b@demo.healthsaathi.test | 8 | 11 | 1 | no |


## Inventory

Generated by `infra/audit/inventory.py`. API routes: 114; called in no run: 1. Requests seen per run: API tests 113 route templates, e2e 61 route templates, demo suite 73 route templates, audit walkthrough 80 route templates.

### API routes

| method | path | auth | called by |
|---|---|---|---|
| POST | /api/v1/access-events/{access_event_id}/query | roles: patient; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/access-events/{access_event_id}/receipt | roles: patient; clinic token; signed in | API tests, demo suite, audit walkthrough |
| GET | /api/v1/access-queries | roles: clinic_admin; clinic token; signed in | API tests, demo suite, audit walkthrough |
| PATCH | /api/v1/access-queries/{query_id} | roles: clinic_admin; clinic token; signed in | API tests |
| GET | /api/v1/alerts | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/alerts/days/{day} | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/alerts/{alert_id} | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/alerts/{alert_id}/review | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/allergies/{allergy_id} | roles: doctor, nurse; clinic token; signed in | API tests |
| GET | /api/v1/appointments | roles: clinic_admin, doctor, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/appointments | roles: clinic_admin, doctor, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/appointments/{appointment_id} | roles: clinic_admin, doctor, reception; clinic token; signed in | API tests, audit walkthrough |
| POST | /api/v1/appointments/{appointment_id}/encounter | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/audit/status | roles: clinic_admin; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/auth/invite/accept | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/login | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/logout | signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/auth/me | signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/otp/request | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/otp/verify | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/password | signed in | API tests |
| POST | /api/v1/auth/refresh | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/select-clinic | signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/auth/sessions | signed in | API tests, demo suite, audit walkthrough |
| DELETE | /api/v1/auth/sessions/{session_id} | signed in | API tests |
| POST | /api/v1/auth/totp/activate | public | API tests |
| POST | /api/v1/auth/totp/enroll | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/auth/totp/verify | public | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/break-glass | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/break-glass/{break_glass_id}/review | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/care-team | roles: clinic_admin, doctor; clinic token; signed in | API tests |
| POST | /api/v1/care-team/{assignment_id}/end | roles: clinic_admin, doctor; clinic token; signed in | API tests |
| GET | /api/v1/clinic | roles: clinic_admin, doctor, lab_tech, nurse, patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/conditions/{condition_id} | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/consent-notices | roles: clinic_admin, doctor, lab_tech, nurse, patient, reception; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/consent-notices | roles: clinic_admin; clinic token; signed in | API tests |
| GET | /api/v1/consent-notices/current | roles: clinic_admin, doctor, lab_tech, nurse, patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/consent-notices/{notice_id}/publish | roles: clinic_admin; clinic token; signed in | API tests |
| POST | /api/v1/consents/{consent_id}/withdraw | roles: patient, reception; clinic token; signed in | API tests |
| GET | /api/v1/dev/mailbox | public | API tests, demo suite, audit walkthrough |
| GET | /api/v1/documents/{document_id} | roles: doctor, lab_tech, patient; clinic token; signed in | API tests |
| POST | /api/v1/encounters/{encounter_id}/close | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/health | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/internal/anchor/run | public | API tests |
| GET | /api/v1/invoices | roles: clinic_admin, reception; clinic token; signed in | API tests |
| GET | /api/v1/invoices/{invoice_id} | roles: clinic_admin, patient, reception; clinic token; signed in | e2e, demo suite, audit walkthrough |
| POST | /api/v1/invoices/{invoice_id}/issue | roles: reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/invoices/{invoice_id}/payments | roles: reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/invoices/{invoice_id}/receipt | roles: clinic_admin, patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/invoices/{invoice_id}/void | roles: reception; clinic token; signed in | API tests |
| PATCH | /api/v1/lab-orders/{order_id} | roles: doctor, lab_tech; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/lab-orders/{order_id}/print | roles: doctor, lab_tech, patient; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/lab-orders/{order_id}/release | roles: doctor, lab_tech; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/lab-orders/{order_id}/result | roles: lab_tech; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/lab/worklist | roles: lab_tech; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/me/patient | roles: patient; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/notes/{note_id} | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/notes/{note_id}/history | roles: doctor; clinic token; signed in | API tests |
| POST | /api/v1/notes/{note_id}/sign | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/notes/{note_id}/versions | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/openapi.json | public | **not tested** |
| GET | /api/v1/patients | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients | roles: clinic_admin, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/patients/{patient_id} | roles: clinic_admin, doctor, lab_tech, nurse, patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/patients/{patient_id} | roles: clinic_admin, doctor, reception; clinic token; signed in | API tests |
| GET | /api/v1/patients/{patient_id}/access-log | roles: clinic_admin, patient; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/allergies | roles: doctor, nurse; clinic token; signed in | API tests |
| POST | /api/v1/patients/{patient_id}/break-glass | roles: doctor, nurse; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/patients/{patient_id}/care-team | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/patients/{patient_id}/chart | roles: clinic_admin, doctor, lab_tech, nurse, patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/chart | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/conditions | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/patients/{patient_id}/consents | roles: clinic_admin, doctor, patient, reception; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/consents | roles: patient, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/patients/{patient_id}/emergency | roles: doctor, nurse; clinic token; signed in | API tests |
| GET | /api/v1/patients/{patient_id}/export | roles: patient; clinic token; signed in | API tests, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/follow-ups | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/invoices | roles: reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/lab-orders | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/notes | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/portal-access | roles: reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/prescriptions | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/patients/{patient_id}/referrals | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/patients/{patient_id}/referrals | roles: doctor; clinic token; signed in | API tests |
| POST | /api/v1/patients/{patient_id}/vitals | roles: doctor, nurse; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/prescriptions/{rx_id} | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/prescriptions/{rx_id}/print | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/prescriptions/{rx_id}/sign | roles: doctor; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/prescriptions/{rx_id}/versions | roles: doctor; clinic token; signed in | API tests |
| GET | /api/v1/push/key | public | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/push/subscriptions | signed in | API tests |
| POST | /api/v1/push/subscriptions/remove | signed in | API tests |
| GET | /api/v1/queue | roles: clinic_admin, doctor, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/queue/tokens | roles: reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/queue/tokens/{token_id} | roles: doctor, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/ready | public | API tests, demo suite, audit walkthrough |
| PATCH | /api/v1/referrals/{referral_id} | roles: doctor; clinic token; signed in | API tests, audit walkthrough |
| GET | /api/v1/reports/daily-revenue | roles: clinic_admin, reception; clinic token; signed in | API tests, demo suite, audit walkthrough |
| GET | /api/v1/schedules | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/schedules | roles: clinic_admin; clinic token; signed in | API tests |
| PATCH | /api/v1/schedules/{schedule_id} | roles: clinic_admin; clinic token; signed in | API tests, audit walkthrough |
| GET | /api/v1/services | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | e2e, demo suite, audit walkthrough |
| POST | /api/v1/services | roles: clinic_admin; clinic token; signed in | API tests |
| PATCH | /api/v1/services/{service_id} | roles: clinic_admin; clinic token; signed in | API tests, audit walkthrough |
| GET | /api/v1/shifts | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | API tests, demo suite, audit walkthrough |
| POST | /api/v1/shifts | roles: clinic_admin; clinic token; signed in | API tests |
| POST | /api/v1/shifts/{shift_id}/cancel | roles: clinic_admin; clinic token; signed in | API tests |
| GET | /api/v1/staff | roles: clinic_admin, doctor, lab_tech, nurse, reception; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/staff/invites | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| GET | /api/v1/staff/members | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| PATCH | /api/v1/staff/members/{membership_id} | roles: clinic_admin; clinic token; signed in | API tests, audit walkthrough |
| POST | /api/v1/staff/members/{membership_id}/invite | roles: clinic_admin; clinic token; signed in | API tests, e2e, demo suite, audit walkthrough |
| POST | /api/v1/walkins | roles: reception; clinic token; signed in | API tests, audit walkthrough |
| WS | /api/v1/ws | public | API tests |

### Screens (web routes)

| path | component |
|---|---|
| / |  |
| /login | LoginPage |
| /login/totp | TotpPage |
| /login/enrol | EnrolPage |
| /select-clinic | SelectClinicPage |
| /status | HomePage |
| /verify | VerifyPage |
| /bench/receipt | ReceiptBenchPage |
| /invite |  |
| /dev/mailbox | DevMailboxPage |
| /patient/login | PatientLoginPage |
| /reception | ReceptionHome |
| /reception/patients/$patientId | ReceptionPatient |
| /nurse | NurseQueue |
| /nurse/patients/$patientId | NursePatient |
| /doctor | DoctorQueue |
| /doctor/patients/$patientId | DoctorPatient |
| /print/prescription/$rxId |  |
| /print/lab/$orderId | LabReportPrint |
| /lab | LabWorklist |
| /admin | AdminHome |
| /patient | PatientPortal |
| /patient/print | RecordPrint |
| /reception/invoices/$invoiceId | InvoicePage |
| /account/sessions | SessionsPage |
| /print/receipt/$invoiceId | ReceiptPrint |

### Tabs

| file | tab |
|---|---|
| apps/web/src/features/admin/AdminHome.tsx | alerts |
| apps/web/src/features/admin/AdminHome.tsx | staff |
| apps/web/src/features/admin/AdminHome.tsx | schedules |
| apps/web/src/features/admin/AdminHome.tsx | shifts |
| apps/web/src/features/admin/AdminHome.tsx | services |
| apps/web/src/features/admin/AdminHome.tsx | consent |
| apps/web/src/features/admin/AdminHome.tsx | breakGlass |
| apps/web/src/features/admin/AdminHome.tsx | reports |
| apps/web/src/features/admin/AdminHome.tsx | revenue |
| apps/web/src/features/admin/AdminHome.tsx | audit |

### Worker and startup jobs

| file | function | tests |
|---|---|---|
| apps/api/app/worker.py | anchor_fallback_tick | apps/api/tests/anchor/test_worker.py, apps/api/tests/anchor/test_worker_timer.py |
| apps/api/app/worker.py | anchor_watch_tick | apps/api/tests/anchor/test_worker_timer.py |
| apps/api/app/worker.py | anchor_timer_run | apps/api/tests/anchor/test_worker_timer.py |
| apps/api/app/worker.py | anchor_fallback_loop | apps/api/tests/anchor/test_worker_timer.py |
| apps/api/app/worker.py | detect_tick | apps/api/tests/detect/test_daily.py, apps/api/tests/detect/test_detect.py |
| apps/api/app/worker.py | detect_loop | not tested |
| apps/api/app/worker.py | push_loop | not tested |
| apps/api/app/worker.py | prune_counters_tick | apps/api/tests/test_worker_prune.py |
| apps/api/app/worker.py | prune_loop | not tested |
| apps/api/app/worker.py | main | not tested |
| apps/api/app/startup.py | maintenance_loop | not tested |
| apps/api/app/startup.py | run_startup_tasks | not tested |

### Make targets

| target | help |
|---|---|
| help |  |
| env | create .env with fresh local secrets |
| install | install Python, JS and Solidity dependencies |
| dev | start postgres, api and web with docker compose |
| dev-down |  |
| demo | run the whole app locally in Docker with demo data, then print the logins |
| demo-code | print the current authenticator code of a demo login, EMAIL=... |
| demo-native | the same demo without Docker: local processes, database on Neon (.env.local) |
| demo-native-stop | / demo-reset-native  stop it / wipe the database and start fresh |
| demo-reset-native |  |
| demo-logs |  |
| demo-e2e | Playwright suite against the running demo (flows, crawler, PWA, tamper) |
| demo-measure | API requests per minute per user on the running demo (rate limits on), JSON in apps/web/test-results |
| demo-audit | full app audit walkthrough on the running demo (rate limits on), results in audit-artifacts/ |
| demo-e2e-docker |  |
| demo-e2e-native |  |
| demo-videos | record a video of each of the 9 flows into demo-videos/ |
| demo-screenshots | desktop and phone screenshots of every screen into demo-screenshots/ |
| fhir-validate | export FHIR bundles of 10 demo patients and run the HAPI validator |
| demo-down | stop the demo (keeps its data); demo-reset deletes the demo data |
| demo-reset |  |
| prod-init | ... prod-github  set up the hosted public demo (docs/RUNBOOK.md) |
| prod-azure |  |
| prod-open-db |  |
| prod-close-db |  |
| prod-status |  |
| prod-db |  |
| prod-seed |  |
| prod-chain |  |
| prod-register |  |
| prod-vm-env |  |
| prod-cloudflare |  |
| prod-github |  |
| prod-activity |  |
| prod-logins |  |
| besu-up |  |
| besu-bench |  |
| besu-down |  |
| load-bench-db |  |
| load-run |  |
| load-run-reversed |  |
| load-clean |  |
| bench-receipt-browser |  |
| prod-code |  |
| db-up |  |
| lint | ruff, mypy, eslint, tsc, forge fmt --check |
| lint-py |  |
| lint-js |  |
| lint-sol |  |
| format |  |
| test | pytest, vitest, forge test |
| test-py |  |
| test-js |  |
| e2e |  |
| test-sol |  |
| contracts-local |  |
| contracts-local-stop |  |
| contracts-amoy |  |
| contracts-deploy |  |
| tamper-demo | show a rewritten audit log failing verification (local) |
| tamper-study |  |
| api-types |  |
| vectors |  |
| audit |  |
| migrate | alembic upgrade head |
| seed | load the synthetic demo clinics |
| sim | run the simulator, CONFIG=path/to/config.yaml (default sim/configs/v1.yaml) |
| sim-coverage | explanation coverage per role of a run, RUN=sim/output/<name> |
| sim-audit | separability audit of a run (must pass before experiments) |
| sim-splits | write the four evaluation splits of a run |
| exp | run an experiment, NAME=smoke\|v1, ARGS=\ |
| prereg-audit | audit the 10 registered v1 seeds before registration |
| prereg-stamp | OpenTimestamps proof of the registration commit (ARGS=--upgrade later) |

# Changelog

## Unreleased

### Fewer database round trips for list screens and charts

- List screens (day list, queue, lab worklist, invoices, review queues) recorded each patient's
  access one by one, about 25 database round trips per patient. `record_many` now loads the
  workflow evidence of all listed patients with set-based queries, explains each access with
  the in-memory evidence backend (the engine the seed already uses, checked against the SQL one
  by the shared backend tests), and writes the access events, explanations and audit events with
  one insert per table, under the same per-clinic lock, with the hash chain built in order.
- Charts record their parts (vitals, notes, lab and so on) the same way (`record_resources`);
  the doctor's first part is still recorded on its own, since it may ask for a typed reason.
- Every access takes two round trips fewer: the clock and the clinic timezone come in one query,
  and the chain head and sequence number in one (after the lock, as before).
- What is recorded does not change; accesses recorded together share one clock reading.
  `tests/access/test_batch.py` checks that the batched path gives the same evidence bundles as
  SqlRepository on every template case, and the same access events, explanations, template
  codes, strengths, forgery flags, audit payloads and chain links as recording one by one.
- `infra/demo/request_stats.py` prints request timings (p50, p95 per endpoint) from the API log.
- The web app shows a thin progress bar while any request takes longer than 0.3 s. List loading
  and empty messages sit outside `<ul>` (axe flagged them on slow loads).

### Native demo with the database on Neon

- `make demo-native`, `make demo-native-stop`, `make demo-reset-native`: the whole demo without
  Docker. anvil, the api, the worker and the production web build run as local processes; the
  database is Neon. Connection strings come from `.env.local` (names in `.env.local.example`):
  the pooled one for the api, the direct one for migrations, the seed and the worker.
- The app accepts hosted Postgres URLs (`postgresql://...?sslmode=require`), turns off prepared
  statement caches behind a transaction pooler, checks and recycles pooled connections, and
  retries the first connection while a sleeping Neon compute wakes up. LISTEN uses the direct URL.
- Database roles on hosted Postgres: the login role there is not a superuser, and PostgreSQL 16
  does not let it switch into roles it created, so revision 0001 failed at `SET ROLE migrator`.
  Before any revision runs, `migrations/env.py` now creates `migrator`, `app_rw`, `anchor_job` and
  `researcher_ro` if missing and grants them to the login role with SET and INHERIT. It does
  nothing for a superuser, and no applied revision was changed.
- The demo seed explains past accesses with the in-memory evidence backend (the same engine;
  a test checks both backends give the same evidence) and writes them in batches.
- `python -m hs_demo.native smoke` runs the API and database smoke tests on the Neon branch
  "test": 242 passed.

### P18 hardening

- Detection fits its models up to the database clock, the clock that stamps accesses; a host
  clock slightly behind the database left the latest accesses out of training.

- `POST /auth/password`: change the password with the current one; other sessions are signed
  out and the user gets an email. "Change password" card on the account page; every password
  field has a show and hide button.
- Web push subscriptions accept only the browsers' push services (the worker posts to that URL).
- Uploaded lab files must start with the bytes of their declared type (PDF, PNG, JPEG).
- Receipts use the newest checkpoint that is already anchored on chain; a pending anchor is shown
  as pending, not as a failure.
- CSP without `'unsafe-inline'` styles (print views use a class), `Cross-Origin-Resource-Policy`,
  HSTS and the same headers for the hosted web app (`public/_headers`), the demo (Caddy) and the
  API. `docs/THREAT_MODEL.md` and `docs/SECURITY_CHECKLIST.md`.
- Offline vitals drafts are saved and listed while the browser is offline (they were paused by
  the query library until the connection came back).

### P17 FHIR export

- `app.fhir.bundle` builds a FHIR R4 `collection` Bundle of the patient's record: Patient,
  Organization, Practitioner, Encounter, Observation (vital signs with LOINC codes, lab values),
  MedicationRequest, DiagnosticReport, Consent and AuditEvent, using the NRCeS ABDM profiles
  (ndhm.in 6.5.0) where they exist. The patient download (`GET /patients/{id}/export`) includes it
  as `fhir`, and the portal has a "Download as FHIR" button.
- `make fhir-validate` and the `fhir` CI job export the bundles of 10 seeded patients
  (`hs_demo.fhir_samples`) and run the HAPI FHIR validator 7.0.1 with the IG and no terminology
  server (`infra/fhir/validate.sh`). FHIR R4 export follows NRCeS ABDM profiles, validated
  offline. Not ABDM certified.

### One-command local demo (make demo)

- `make demo` builds and starts `infra/docker-compose.demo.yml`: postgres, anvil with the contracts
  deployed automatically, a one-time seed, the api, the worker (anchor job every 2 minutes,
  detector every minute) and the production web build behind Caddy on http://localhost:8080 with
  the API on the same origin. Ports listen on 127.0.0.1 only. `make demo-code EMAIL=...` prints a
  login's current authenticator code; `make demo-down` and `make demo-reset` stop or wipe it.
- `infra/demo` (package `hs_demo`): runs the unchanged SaathiBench simulator for two polyclinics
  and three weeks, loads 50 patients and all their activity into the product tables with the
  product's own explanation engine and audit chain, registers the clinics on the local chain,
  runs the detector over the past days, books today's queue, anchors once, and prints the login
  table with the insider cases from the simulation. Secrets, the poster wallet and VAPID keys are
  generated per installation in the demo volume.
- The local witness is a git repository in `.demo/witness`; OpenTimestamps is off and shows as
  not configured.

### App gap fixes (docs/APP_GAPS.md)

- Every problem code the API raises is translated in English and Hindi; the UI never shows raw
  server text and falls back to a message per HTTP status. A test fails when a new code has no
  translation.
- "My sessions" page for every signed-in user, with sign out per device.
- `/invite` page where invited staff set their password.
- Email alert when an account signs in from a device it has not used before.
- Doctors can add and resolve conditions, amend signed notes and prescriptions (new version) and
  see note versions, list and close referrals, and manage the care team. The break-glass view
  survives a page reload while it is valid. New `GET /patients/{id}/referrals`.
- Nurses can mark allergies inactive; reception can cancel appointments or mark no-shows; admins
  manage staff shifts.
- Bilingual lab report print view and `GET /lab-orders/{id}/print` (doctor and patient, released
  results only, logged as a print access).
- Web push opt-in for patients (SPEC 9): `GET /push/key`, `POST /push/subscriptions`,
  `POST /push/subscriptions/remove`. When staff open a subscribed patient's record a job is queued
  in the same transaction and the worker sends a notification that carries no patient data. New
  env vars `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (`make env` creates the pair).
- Dev mailbox (`GET /dev/mailbox` and the `/dev/mailbox` page), answering only when
  `APP_ENV=local`. The web app reads runtime settings from `/config.js`.
- Admin alert queue opens on the open alerts of every day; the day view uses the local date.
- Anchoring: `ANCHOR_CHAIN=anvil` for a local chain (stored as the self-hosted `besu` backend) and
  `WITNESS_DIR` for a local git witness folder (stored as `github`). `GET /audit/status` returns
  the state of every witness, configured or not, and the audit page shows each one.

### Results of the registered v1 experiments

- The main run, the 16 ablations and the ECOD and COPOD scorer variants of the registered plan
  (experiments/PREREGISTRATION.md at 14f5feb), 10 seeds each. Summaries, tests, per-seed metric
  rows, configs, run manifests and audits are in `experiments/results/v1/`; the tables and
  figures written by the pipeline are in `paper/tables` and `paper/figures`. How the runs were
  executed, and every change made after registration, is in section 9 of the pre-registration.
- `e2d_experiments.paper_figures` draws the paper figures (vector PDF and 300 DPI PNG) from the
  committed results only; diagnostics that are not registered results are in
  `experiments/results/v1/diagnostics.json` and `diagnostics2.json`.

### Experiments: B3 without full-size sequence copies

- The sequence VAE (B3) samples its 100,000 training accesses before building their sequences
  and normalises test sequences in place. Results are unchanged (regression test against the old
  implementation); the first v1 main run had stopped with an out-of-memory error in B3.

### Forgery flags per type-10 variant

- `make prereg-audit` reports the forgery flag fire rates and ROC-AUCs for each type-10 variant
  (appointment, lab order, care team) as well as pooled, in `experiments/audit/v1/summary.json`.

### off_path_creation uses the status at the access; status history checked in the audit

- `off_path_creation`: an encounter with the booking clinician counts when it started before the
  booking and its appointment was not cancelled or a no-show at the access time (`status_at`),
  never by its final status. Evidence bundles carry each encounter's `appointment_id`.
- The SaathiBench audit fails a run in which any appointment, lab order, referral or invoice lacks
  a full status history, so the stored-status fallback is never used on simulator data.

### Simulator: careful forgers complete a fake visit

- A doctor who forges an appointment (attack type 10) now, with probability m, runs a fake visit
  at the slot: checks the patient in, opens the consultation, writes and signs a short note, and
  the appointment ends completed. Otherwise it becomes a no-show as before. Doctors no longer
  cancel their forged appointments; receptionists and lab technicians still do. Forgery becomes
  harder to detect, never easier. Outputs made before this change were regenerated.

### Bounded memory in simulation and audit runs

- The simulator writes append-only tables every 50,000 rows instead of 200,000. Peak memory of
  the largest v1 clinic (nursing home, seed 20261009) fell from about 960 MB to about 770 MB
  committed; the rows written are the same.
- `SIM_WORKERS` sets how many clinics the simulator and `make prereg-audit` run in parallel
  (default CPUs - 1). The output does not depend on it: v1 seed 20261008 gives byte-identical
  Parquet files with 11 workers and with 1.

### Explanations at access time; off_path_creation redefined

- `status_events` table (migration 0004): append-only status history of appointments, lab orders,
  referrals and invoices, with RLS and tests. A flush listener (`app.db.status_history`) writes a
  row whenever one of these records is created or changes status.
- e2d-core: both repositories load the status history and expose `status_at`. Templates use a
  record's status at the access time; `cancelled_after_access` and `no_progress` use the status 24
  hours after the access, never the final status.
- `off_path_creation`: an appointment booked by a clinician for a patient with no completed
  encounter with that clinician before the booking. The clinic booking norm (`reception_norm`) is
  removed. Changed after seeing the forgery flag table of the pre-registration audit.
- Encounters carry `ended_at` in the evidence bundle.

### Simulator fix: status history, lab order resolution, antenatal registration

- `status_events`: one row per status an appointment, lab order, referral or invoice takes,
  creation included, so the status at any past time can be read without `updated_at`.
- Lab orders: `lab_uncollected_rate` (0.05 in every profile) of real orders are never collected
  and are cancelled at closing time. Forged lab orders (attack type 10) are made by lab
  technicians, since a lab order only explains a lab technician's access, and are cancelled by
  the forger with probability m, otherwise at closing time.
- Antenatal patients registered during the run plan 2 to 7 visits, like those registered before.
- A walk-in is checked in no earlier than its appointment is created.
- `saathibench.realism` reports the share of consultation lab orders that were never collected.
  Outputs made before this change were regenerated.

### Simulator fix: forged appointments resolved like real ones

- Fixed: an appointment forged by attack type 10 could stay booked after its slot, which no real
  appointment does once follow-ups are fixed, so the status alone would have marked it as forged.
  Forged appointments are now resolved like any other: a no-show at the usual time (120 minutes
  after the slot), or, with probability equal to the campaign's mimicry, cancelled by the forger
  after the access and before the slot. Outputs made before this fix were regenerated.

### Simulator fix: follow-ups in later sessions

- Fixed: a follow-up a doctor booked into a later session of the same day (usually the evening)
  was dropped, so the visit never happened and the appointment stayed booked. All SaathiBench
  outputs made before this fix, including the earlier `experiments/audit/v1/` files, had this bug
  and were regenerated.
- `saathibench.realism` counts appointments with a slot inside the run separately from those
  after the last day, which stay booked by design.

### Pre-registration alignment

- e2d-core: a patient registration never sets `self_created_recent` (registering and then
  opening the record is the normal front-desk path); its other forgery flags still apply.
- `BEHAVIOUR_COLUMNS` in `e2d_core.features.columns`, shared by the separability audit and the
  raw-feature baselines. Experiment configs moved to `experiments/configs/`.
- Simulator realism settings in the run config (`realism`: hard-negative scale, no-show, cancel and
  cover-day overrides; `attacks.incidental_share`), a config fingerprint in every manifest so an
  old run is never reused, and `saathibench.realism` measures.
- Metrics as registered: type 8 left out of the budgeted metrics and reported as review-queue
  outcomes, alerts raised, alerts timed when their features are available (24 hours for the
  forgery flags), mimicry high at m >= 1/2, explanation-signal ROC-AUCs, per-clinic counts for a
  cluster bootstrap over clinics.
- Statistics: H2 primary test (temporal, B = 5) uncorrected and the other cells Holm-corrected,
  zero differences counted, H3 seed drops counted, H5 (E2D against explanation only).
- Ablations `hard_neg_half` and `hard_neg_double`.
- `make prereg-audit` (audits the 10 registered seeds into `experiments/audit/v1/`) and
  `make prereg-stamp` (OpenTimestamps proof of the registration commit).

### P15 - Experiment pipeline

- `experiments/`: Hydra configs (`experiments/configs`) for the data (smoke, v1), every ablation
  (no forgery, no graph, role conditioning, learning curves, core templates, mimicry levels,
  behaviour windows) and the run settings (seeds, splits, budgets, methods).
- Pipeline: simulate each seed, require the separability audit (v1), write the splits, explain
  every access with the shared engine (forgery flags evaluated 24 hours later), build features.
  Preparation code cannot import labels (import-linter).
- Methods: B0 rules, B1 and B2 IsolationForest, B3 sequence VAE, B4 co-access collaborative
  filtering, B5 explanation only, E2D (IsolationForest, ECOD or COPOD), LightGBM upper bound.
- Metrics (recall and precision at B, PR-AUC, campaign detection, hours to first alert, alerts
  per 1,000, coverage, false-explanation rate, recall per attack type and mimicry band),
  statistics (bootstrap intervals, one-sided paired Wilcoxon with Holm correction, rank-biserial
  effect size, H1 to H4), LaTeX tables, figures and local MLflow logging.
- `make exp NAME=smoke|v1 ARGS="..."` and `python -m e2d_experiments.report` for the hypotheses.
- `experiments/PREREGISTRATION.md` drafted for the authors to commit before any final run.

### P14 - Features, scorer and detector in the product

- e2d-core features (SPEC 5.4): explanation strength, template one-hot and break-glass; forgery
  flags; graph features (direct care link, care-graph distance, team overlap, staff or own record,
  shared surname or address, days since last visit). One builder over table-shaped frames, used by
  the simulator output and the product alike.
- e2d-core detection (SPEC 5.5): gate, rules baseline, IsolationForest, ECOD and COPOD scorers,
  per-clinic fitting with a global fallback, threshold calibration, daily top-B budget, model files
  versioned by sha256.
- Product: the worker scores each clinic's accesses every minute, raises at most ALERT_BUDGET
  alerts per clinic and day, pushes `alerts.new` over WebSocket, and refits models daily.
  `python -m app.detect.job [--fit]` runs either by hand.
- API: `GET /alerts`, `GET /alerts/{id}` (recorded as an access, with the user's last 50 accesses
  as pseudonymous timeline) and `POST /alerts/{id}/review` (audited; no self-review).
- Web: Alerts tab for clinic admins with explanation, standout features, forgery signs, timeline
  and review buttons, in English and Hindi.
- Playwright flows 7 (break-glass and its review) and 8 (alert review).
- Prod: the worker keeps fitted models on a volume.
- New env vars: `MODEL_DIR`, `ALERT_BUDGET`, `DETECT_SCORER`, `DETECT_INTERVAL_SECONDS`.
- ADR 0008 records the design.

### P13 - Attacks, mimicry, separability audit

- Ten insider attack types (SPEC 7) as campaigns inside the simulation, each with a mimicry level
  that moves its timing toward the actor's own working hours and spreads its volume. Forged
  evidence, break-glass events, note versions and new devices go into the normal tables.
- Labels for every access (`labels/access_labels`) and per campaign (`labels/campaigns`), read
  only through `saathibench.labels`. import-linter contracts in `make lint` keep e2d-core and the
  product independent of the simulator and keep labels away from feature code.
- e2d-core behaviour features (SPEC 5.4): per-user rolling accesses, distinct patients and exports
  over 1 h, 24 h and 7 d, off-shift, new device and session age.
- `make sim-audit`: single-feature and depth-2 tree ROC-AUC gate at 0.9, without explanation
  features. `make sim-splits`: temporal, clinic, attack and user splits.
- Staff relatives are registered as patients of their clinic (same surname and address), and
  month-end billing is shared between receptionists with a per-person limit.
- ADR 0007 records the simulator design.

### P12 - SaathiBench simulator (benign)

- `sim/`: SimPy model of solo GP, polyclinic and nursing home clinics (`sim/profiles`), with
  sessions, booked and walk-in visits, reception, nurse, doctor and billing queues, lab work,
  referrals, follow-ups, health camps, cover days and patient portal use. Writes the product's
  table shapes to Parquet and the run's settings and row counts to `manifest.json`.
- Benign hard negatives: covering doctor, after-hours break-glass, nurse lab follow-up, pharmacy
  check, month-end billing and staff viewing their own record, listed in
  `labels/benign_scenarios`, kept apart from the tables.
- Allow and deny decisions and `policy_version` come from the product's policy file.
- `make sim CONFIG=...` and `make sim-coverage RUN=...` (explanation coverage per role and per
  scenario from the shared e2d-core engine). `sim/DATA_CARD.md` draft.
- e2d-core: the in-memory evidence repository now builds per-patient and per-user indexes once
  and answers each query with binary searches, giving the same results as before (the shared
  backend tests and a comparison on simulator output both check this).
- The simulator is type-checked with mypy and its tests run in `make test`.

### P11 - Deployment

- Prod-demo: `infra/docker-compose.prod.yml` (API, worker, Caddy with automatic HTTPS, one-shot
  migrate) and `infra/deploy/deploy.sh`, which migrates, switches to the new image, checks
  `/ready` and goes back to the previous image if the new one is not healthy.
- `.github/workflows/deploy.yml`: staging after CI passes on `main` (Neon migrations, Render deploy
  hook, Cloudflare Pages preview) and prod-demo on `v*` tags that passed CI (image over SSH to the
  VM, Pages production). Steps are skipped until their secrets are set.
- `.github/workflows/nightly.yml`: encrypted `pg_dump` to the private backup repository (last 14
  kept) and an `alembic check` schema drift check against staging. Nightly rescoring is added with
  the detector in P14.
- `render.yaml` for the staging API, `apps/web/public/_headers` for Cloudflare Pages.
- The production image runs as a non-root user, calls the virtualenv directly and has a health
  check. CI builds it, checks the compose file and runs shellcheck on the deploy scripts.
- Alembic reads `MIGRATOR_DATABASE_URL` without needing the app secrets, and `alembic check`
  ignores monthly partitions.
- `docs/RUNBOOK.md`: database logins (the app login can only switch into `app_rw` and
  `anchor_job`), deploy, rollback, secret rotation, anchoring, backup restore and free-tier checks.
- New env vars: `API_DOMAIN`, `BACKUP_DATABASE_URL` (VM only).

### P10 - Anchoring, receipts and verification

- Anchor job: signs a checkpoint for each clinic with new audit events, posts it to AuditAnchor,
  commits it to the GitHub witness repository and stamps it with OpenTimestamps at most once an
  hour, upgrading pending stamps later. Safe to run twice at once and picks up interrupted runs.
  Refuses to anchor a log whose history no longer matches the root on chain.
- Triggers: CLI, a token-protected `POST /internal/anchor/run` called every 15 minutes by
  `.github/workflows/anchor.yml`, and a fallback timer in the new worker process.
- Patient receipts: `GET /access-events/{id}/receipt`, checked in the browser with
  `packages/receipt-verify` and viem (payload hash, inclusion proof, signature, registered key,
  on-chain root). "Check receipt" on each access log entry and a public `/verify` page.
- Admin audit tab: `GET /audit/status` with the latest checkpoint, hash chain and consistency
  checks, and links to each witness.
- `app.scripts.register_clinic` for the two-of-three ClinicRegistry registration.
- `make tamper-demo` rewrites an audit row on a local chain and shows verification fail.
- Migration 0003: lets `anchor_job` record a clinic's signer key, and indexes access audit rows.
- CI runs the anchoring tests against anvil.
- New env vars: `ANCHOR_RPC_URL`, `ANCHOR_CHAIN`, `AUDIT_ANCHOR_ADDRESS`,
  `CLINIC_REGISTRY_ADDRESS`, `ANCHOR_POSTER_PRIVATE_KEY`, `ANCHOR_EXPLORER_URL`,
  `ANCHOR_TX_TIMEOUT_SECONDS`, `ANCHOR_TRIGGER_TOKEN`, `ANCHOR_FALLBACK_MINUTES`, `WITNESS_REPO`,
  `WITNESS_BRANCH`, `WITNESS_GITHUB_TOKEN`, `OTS_ENABLED`, `OTS_CALENDARS`, and the web
  `VITE_CHAIN_RPC_URL`, `VITE_AUDIT_ANCHOR_ADDRESS`, `VITE_CLINIC_REGISTRY_ADDRESS`,
  `VITE_EXPLORER_URL`.

### P9 - Web app: lab, billing, admin, patient portal

- Lab worklist: sample collected, results with an optional PDF or image report, release to the
  doctor. Doctors see results and open reports from the chart.
- Billing: invoice from the price list, issue, payments, bilingual receipt print view.
- Clinic admin: staff list, invites and deactivation (ends their sessions), schedules, services,
  consent notice versions, break-glass review, accesses reported by patients, daily revenue.
- Patient portal: sign in with an email code, own record (signed notes and prescriptions, released
  results, bills), access log grouped by day with a plain sentence for each access in English or
  Hindi, "I do not recognise this", consent withdrawal and history, data download as JSON and a
  print view. Reception can give a patient portal access by email.
- API: `/me/patient`, `/patients/{id}/access-log`, `/access-events/{id}/query`,
  `/access-queries`, `/patients/{id}/export`, `/patients/{id}/portal-access`, `/staff/members`.
- Fixed: a request's transaction is now committed before its response is sent (found by the
  browser tests; see ADR 0004).
- Playwright tests for SPEC flows 1 to 6.
- New env var: `EMAIL_OUTBOX_DIR` (local and test only).

### P8 - Web app: sign in, reception, nurse, doctor

- Sign in with password and authenticator code, authenticator set-up with QR code, clinic choice,
  silent session restore from the refresh cookie, sign out that clears cached data and drafts.
- Reception: patient search, registration with the consent notice shown and agreed, booking,
  walk-ins and queue tokens. Nurse: today's queue, vitals and allergies. Doctor: queue, chart with
  the reason prompt and break-glass, consultation start and finish, notes with signing,
  prescription builder, lab orders, referrals and follow-ups.
- Bilingual prescription print view (Noto Sans and Noto Sans Devanagari, browser print).
- Typed API client generated from the OpenAPI document (`make api-types`); a test fails if the
  committed document is out of date. Live updates over the WebSocket.
- Offline: the app shell and today's queue are cached; vitals saved without a connection are kept
  on the device encrypted with a key that only lives in memory for the session.
- Playwright tests for SPEC flows 1 to 3 at 360 px wide (`make e2e`, and a CI job). Installability
  is checked with Chrome's own `Page.getInstallabilityErrors` on the production build, because
  Lighthouse no longer has a PWA category.

### P7 - Clinical, billing and consent APIs

- Consultations (encounters), vitals with range checks, allergies, conditions, clinical notes
  (draft, sign, new version; body encrypted with AES-256-GCM), prescriptions (draft, sign, new
  version, print), lab orders and results with an uploaded report, referrals and follow-ups.
- Migration `0002`: a database trigger rejects any change to a signed note or prescription.
- Lab report files go through one storage interface: a local folder in development, Azure Blob
  (container SAS URL) in production. The sha256 of every file is stored and checked on download.
- Billing: price list, invoices, payments (cash, UPI, card on an offline terminal), receipt view,
  daily revenue report.
- Consent: versioned notices in English and Hindi, consent at registration, withdrawal by the
  patient, history with events. Each consent stores an HMAC commitment for later anchoring.
- Every write records an access event and an audit event; the audit payload carries the operation
  and record ids (never clinical content).
- New env vars: `STORAGE_DIR`, `AZURE_BLOB_CONTAINER_SAS_URL`, `MAX_UPLOAD_BYTES`.

### P6 - Full explanation engine, forgery flags, task views, break-glass

- All eleven templates from SPEC 5.2 (T_APPT, T_QUEUE, T_FRONTDESK, T_LAB, T_REFERRAL, T_CARETEAM,
  T_FOLLOWUP, T_BILLING, T_REASON, T_BREAKGLASS, T_SELF) and all six forgery indicators from
  SPEC 5.3, with parameters in `templates.yaml`. See ADR 0005.
- Both repository backends now return encounters, lab orders with release times, referrals, care
  team assignments, break-glass events, care progress, clinic hours, the clinic booking norm and
  per-user evidence creation counts.
- `GET /patients/{id}/chart` returns a different schema per role (reception, nurse, lab, doctor,
  admin) and records one access per resource type shown.
- Doctor reason flow: 428 when the chart is not explained, then `POST /patients/{id}/chart` with a
  typed reason (T_REASON).
- Break-glass: emergency view for doctors and nurses, valid 4 hours, and an admin review queue.
- Table-driven tests run every template and flag on both backends.

### P5 - Clinic operations and access recording

- APIs for patients (register, search with cursor pagination, view, edit), staff directory,
  schedules, shifts, appointments, walk-ins, queue tokens and care team assignments.
- `access.record_and_explain()` writes the access event, its explanation and the audit event in
  the same transaction as each read or write; denials are logged too. See ADR 0004.
- e2d-core explanation engine with T_APPT, T_QUEUE, T_FRONTDESK and T_SELF (weights and windows in
  `templates.yaml`), and the evidence repository with SQL and polars backends that pass one shared
  test suite.
- Access policy in `apps/api/app/access/policy.yaml`; its sha256 is stamped on every access event
  and stored in `policy_versions` at startup.
- WebSocket `/api/v1/ws` (token in the first message) with queue and appointment updates through
  Postgres LISTEN/NOTIFY.
- Monthly partitions are created at startup and every 12 hours.

### P4 - Smart contracts

- `ClinicRegistry`: registers a clinic's signer key hash and poster address, and rotates keys
  with history. Every change needs two of the three admins to send the same call.
- `AuditAnchor`: only the clinic's poster can anchor; tree size must increase and `prevRoot` must
  equal the last anchored root. Emits `Anchored(clinicId, treeSize, root, sthDigest, timestamp)`.
- `ConsentRegistry`: optional append-only consent commitments, tested but not used yet.
- Unit and fuzz tests (1024 runs, fixed seed), committed gas snapshot checked in CI, Slither in CI.
- Deploy script for anvil (`make contracts-local`, uses anvil's unlocked accounts), Polygon Amoy
  (`make contracts-amoy`) and any RPC (`make contracts-deploy NETWORK=...`). Addresses are written
  to `contracts/deployments/<network>.json`.
- New env vars: `AMOY_RPC_URL`, `CUSTOM_RPC_URL`, `DEPLOYER_PRIVATE_KEY`, `REGISTRY_ADMIN_1..3`.

### P3 - Ledger: canonical JSON, hash chain, Merkle, signed tree heads

- `e2d_core.ledger`: RFC 8785 canonical JSON, the per-clinic hash chain from SPEC 6, RFC 6962
  Merkle trees with inclusion and consistency proofs, and Ed25519 signed tree heads with key ids.
  Clinic signing keys are derived from one seed with HKDF.
- Property tests (hypothesis) against a naive RFC 6962 reference, the Certificate Transparency
  test roots, and the RFC 8785 examples.
- `apps/api/app/ledger`: audit event writer (advisory lock per clinic, same transaction as the
  caller) and a checkpoint service that re-checks the chain and the previous root before signing.
  Inclusion proofs for a single audit event.
- `packages/receipt-verify`: the same inclusion, consistency and signature checks in TypeScript.
  Both sides are tested against `packages/receipt-verify/test/vectors/ledger.json`, generated by
  `make vectors`; a Python test fails if the committed file is out of date.
- `make test` now fails if e2d-core line coverage drops below 90%.
- New env var: `LEDGER_SIGNING_SEED`.

### P2 - Identity, sessions, MFA

- Staff sign in with password then TOTP; staff without TOTP must enrol first. Patients sign in
  with a 6 digit email code (10 minutes, 5 attempts). See ADR 0003.
- 15 minute access tokens, rotating refresh token in an HttpOnly cookie, reuse of a rotated token
  ends the whole session, sessions list and revoke, sign out, clinic selection.
- Clinic admins invite staff by email; the invitee sets a password (minimum 10 characters, not a
  common password) and then enrols TOTP.
- Rate limits from SPEC 4 (slowapi, in memory), account lockout after 5 wrong passwords, TOTP codes
  accepted once, security headers, RFC 7807 problem+json errors.
- AES-256-GCM key ring with key ids for data at rest.
- New env vars: `JWT_SECRET`, `OTP_HMAC_KEY`, `DATA_KEYS`, `REFRESH_TOKEN_DAYS`, `COOKIE_DOMAIN`,
  `COOKIE_SECURE`, `RATE_LIMIT_ENABLED`, `PUBLIC_APP_URL`, `EMAIL_BACKEND`, `BREVO_API_KEY`,
  `EMAIL_FROM`, `EMAIL_FROM_NAME`. `make env` writes a local `.env` with random secrets.

### P1 - Data model, migrations, RLS, DB roles

- SQLAlchemy models for every table in SPEC 3.1 with UUIDv7 ids and `timestamptz` columns.
- Alembic migration `0001` creates the roles `migrator`, `app_rw`, `anchor_job` and `researcher_ro`,
  row level security on every clinic scoped table, composite same-clinic foreign keys, monthly
  partitions for `access_events` and `audit_events` with `create_month_partitions()`, append-only
  triggers, and the `research.access_events_deid` view. See ADR 0002.
- Every app transaction runs as `app_rw` with `app.clinic_id`, `app.user_id` and `app.role` set
  locally (`app.core.db.tenant_session`).
- Tests: per-table read, insert, update and delete isolation between two clinics, append-only
  checks, role checks, and a schema drift check against `information_schema` and Alembic.
- `make migrate` and `make seed` (two demo clinics, staff for every role, 50 synthetic patients,
  schedules, services and consent notice v1 in English and Hindi).
- New env vars: `MIGRATOR_DATABASE_URL`, `SEED_DEMO_PASSWORD`.
- Still to do: call `create_month_partitions()` from the background worker on a schedule (P5).

### P0 - Repo scaffold, tooling, CI

- Monorepo with a uv workspace (apps/api, packages/e2d-core, sim, experiments) and a pnpm workspace
  (apps/web, packages/receipt-verify).
- API: FastAPI app with `/api/v1/health` and `/api/v1/ready` (checks the database), settings from
  environment, JSON logs with a request id on every line and in the `x-request-id` header.
- Web: Vite, React 19, strict TypeScript, Tailwind, shadcn/ui base components, TanStack Router and
  Query, i18next with English and Hindi, installable PWA with an update prompt. The home page shows
  the API health.
- Contracts: empty Foundry project with forge-std.
- Dev stack in `infra/docker-compose.dev.yml` (postgres 16, api with hot reload, web).
- Makefile, CI workflow (ruff, mypy, pytest with postgres, eslint, tsc, vitest, forge, pip-audit,
  pnpm audit), pre-commit hooks, editorconfig, `.env.example`, ADR 0001.

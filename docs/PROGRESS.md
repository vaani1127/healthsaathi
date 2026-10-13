# HealthSaathi: progress report

For the mentor review, October 2026. All data in this project is synthetic.

## What it is

HealthSaathi is a clinic management web app for small Indian OPD clinics (1 to 10 doctors), in
English and Hindi, that can be installed on a phone. Its research idea is Explain-then-Detect
(E2D): every time a staff member opens a patient's record, the app looks for the clinic workflow
that explains it (an appointment, a queue token, a lab order, a referral, the care team,
emergency access). Accesses that are not explained, or whose explanation looks forged, are ranked
by an anomaly detector and shown to the clinic admin a few at a time. Every access is written to
an append-only log whose signed checkpoints are published to outside witnesses, and patients can
check their own access receipts in the browser.

## What is built

| Module | State |
|---|---|
| Sign-in and accounts | Email and password with an authenticator app (TOTP) for staff, email codes for patients, account lock, sessions list with sign-out per device, new-device email, password change |
| Clinic work | Patients, consent notice at registration (English and Hindi), appointments, walk-ins, queue tokens, schedules, shifts, care team |
| Clinical | Vitals (also offline), allergies, conditions, consultation notes and prescriptions with signing and versions, lab orders and results, referrals, follow-ups, bilingual print views (prescription, lab report, receipt) |
| Billing | Services, invoices, payments, receipt, daily revenue |
| Access explanation (E2D) | Every read and write of patient data is recorded with its explanation in the same database transaction; doctors without an explanation must type a reason; break-glass with a 24-hour review queue |
| Detection | Isolation forest per clinic on explanation, forgery, graph and behaviour features; a small daily alert budget; review workflow |
| Audit log and anchoring | Hash chain, signed Merkle checkpoints, anchoring on an EVM chain (Polygon Amoy in production, a local chain in the demo), a git witness, OpenTimestamps; tamper evidence shown on the admin audit page |
| Patient portal | Records, access log with plain-language reasons, "I do not recognise this", in-browser receipt verification, consent withdrawal, data download (JSON and FHIR), push notification opt-in |
| FHIR | R4 export following the NRCeS ABDM profiles, validated offline with the HAPI validator. Not ABDM certified |
| Security | Row level security per clinic in Postgres, role-based task views, append-only audit table, encryption of notes and TOTP secrets, CSP and security headers. OWASP ASVS level 1 checklist and threat model in `docs/` |
| Local demo | `make demo` (Docker) or `make demo-native` (no Docker, database on Neon) runs everything on one laptop: two clinics, 50 patients and about three weeks of simulated activity, including simulated insiders |

## Test numbers

| Suite | Result |
|---|---|
| Python (API, E2D core, simulator, experiments) | 807 passed (CI run 38441215530, 13 October 2026); E2D core line coverage 99.7% |
| JavaScript (web app and receipt verification) | 82 passed |
| Solidity contracts | 51 passed |
| Browser tests against the demo stack (all 9 flows, a crawler of every screen at desktop width, phone width and in Hindi, offline use, accessibility, tamper demo) | 55 of 55 passed; on 13 October 2026 in native mode with the database on Neon, in 37.8 minutes |
| FHIR bundles of 10 patients, HAPI validator | 10 of 10 with 0 errors |
| OWASP ZAP baseline scan (web and API) | 0 failures |
| axe accessibility (WCAG 2.1 A and AA, English and Hindi) | 0 violations |

## Demo speed on Neon

The demo on this laptop runs without Docker and keeps its database on Neon in Singapore, so every
screen waits for round trips to it. In a deployment the API and the database sit in the same
data centre. Timings per API request over the full browser test suite, from
`infra/demo/request_stats.py` on the API log, 13 October 2026:

| Request | Before batching (589 requests) | After batching (816 requests) |
|---|---|---|
| All requests, median and 95th percentile | 3.58 s and 39.65 s | 2.57 s and 8.99 s |
| Reception day list, median | 40.97 s | 5.86 s |
| Patient chart, median | 34.95 s | 8.28 s |
| Queue, median | 15.72 s | 5.38 s |

"Batching": list screens and charts used to record each patient's access, and each part of a
chart, one by one; they now load the evidence for all of them at once and write the log rows in
one insert per table. What is recorded did not change (`apps/api/tests/access/test_batch.py`).

## Research results

The experiment plan was pre-registered before any method was run: commit `14f5feb` (tag
`prereg-v1`, 11 October 2026), with an OpenTimestamps proof anchored in Bitcoin block 970875
(`experiments/PREREGISTRATION.ots`). The final runs used the SaathiBench simulator (30 clinics,
180 days, 10 seeds). Every number below is from `docs/results-v1.md`, which is generated from the
committed result files.

| Hypothesis | Result |
|---|---|
| H1: explanations cover at least 90% of normal accesses, and explain fewer than 10% of attacks | Not supported. Coverage 0.970 (met), but 0.172 of attack accesses were explained (not met). Most of these are insiders who are on the patient's care team, which an explanation cannot rule out |
| H2 (primary): E2D finds more attacks than the best baseline at 5 alerts per clinic per day | Supported. Recall 0.216 against 0.122 for the rules baseline, p = 0.00098; E2D was also higher in all 11 other split and budget settings |
| H3: the gain is larger for hard attacks (relative and colleague snooping, low-and-slow) | Supported. Difference of gains 0.178, p = 0.00098 |
| H4: forgery features help find forged explanations | Supported. Recall on forgery attacks 0.372 against 0.230 without them (temporal split) and 0.357 against 0.225 (attack split), Holm p = 0.0020 |
| H5: the detector adds to explanations alone | Supported. 0.216 against 0.140, p = 0.00098 |

Weaknesses reported with the results: absolute recall is modest (about one attack access in five
at five alerts a day), alerts come about a day after the access, and for careless attackers
simple rules do as well as E2D.

## What remains

- Hosting: deploy to Azure for Students and Cloudflare Pages, fund the Polygon Amoy wallet,
  create the public GitHub witness repository and turn on OpenTimestamps. These need accounts
  and secrets that only the project owner can create.
- Paper: not started. A fuller literature search (Google Scholar and the ACM Digital Library) is
  needed before writing the related work and the novelty claim.
- Smaller items: password recovery for staff, a password strength meter, and a 12-character
  minimum password (the specification currently says 10).

Designed to support obligations under the DPDP Act 2023 and DPDP Rules 2025. Not legally
reviewed or certified.

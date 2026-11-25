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
| Audit log and anchoring | Hash chain, signed Merkle checkpoints, anchoring on an EVM chain (Polygon Amoy in production, a local chain in the demo), a git witness, OpenTimestamps; the admin audit page checks the log against the stored checkpoints and against the newest head on the public witness, so deleting the stored checkpoints does not hide a rewrite |
| Patient portal | Records, access log with plain-language reasons, "I do not recognise this", in-browser receipt verification, consent withdrawal, data download (JSON and FHIR), push notification opt-in |
| FHIR | R4 export following the NRCeS ABDM profiles, validated offline with the HAPI validator. Not ABDM certified |
| Security | Row level security per clinic in Postgres, role-based task views, append-only audit table, encryption of notes and TOTP secrets, CSP and security headers. OWASP ASVS level 1 checklist and threat model in `docs/` |
| Local demo | `make demo` (Docker) or `make demo-native` (no Docker, database on Neon) runs everything on one laptop: two clinics, 50 patients and about three weeks of simulated activity, including simulated insiders |

## Test numbers

| Suite | Result |
|---|---|
| Python (API, E2D core, simulator, experiments) | 892 passed (CI run 42959698908, 11 November 2026, commit 0b455e4); E2D core line coverage 99.7% (1,370 of 1,374 lines) |
| JavaScript (web app and receipt verification) | 88 passed (25 web app, 63 receipt verification; same CI run) |
| Solidity contracts | 51 passed (same CI run) |
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

## Live demo

Since 31 October 2026 the app runs as a public demo with synthetic data only: the web app at
https://app.vaaniprashar.tech (Cloudflare Pages) and the API at https://api.vaaniprashar.tech (an
Azure VM and Azure PostgreSQL in East Asia). Checkpoints are anchored on Polygon Amoy, published
to the public GitHub repository vaani1127/healthsaathi-witness and stamped with OpenTimestamps. A
synthetic activity bot makes visits during clinic hours so the log keeps growing. Set-up steps
are in `docs/RUNBOOK.md`.

The deployed release is v0.1.6: the deploy finished on 11 November 2026, with the worker started
at 09:29:54 UTC (14:59:54 IST), between OPD sessions. It changes only the anchor staleness
warning. The fix for the anchor timer that stalled from 5 to 10 November came in v0.1.5 (worker
started 10 November 2026, 18:47:10 UTC); see `docs/CHANGELOG.md`.

## Anchoring and overhead study (P16)

Every result is a committed file written by a script; no numbers are typed by hand.

| Part | Script | Results |
|---|---|---|
| Local 4-validator Besu QBFT network, 200 anchors | `make besu-bench` (`hs_demo.besu`) | `experiments/results/anchoring/besu` |
| Receipt check time in the browser (laptop, Android phone) | `/bench/receipt` page | `experiments/results/anchoring/browser` |
| Which check catches a rewritten log | `make tamper-study` | `experiments/results/anchoring/tamper/detection.json` |
| Latency with and without access recording, both run orders | `make load-run`, `make load-run-reversed`, `python -m e2d_experiments.load` | `experiments/results/load`, `paper/tables/load_overhead.tex` |
| Rewrite window and gas on the live demo | `python -m e2d_experiments.anchoring`, `python -m e2d_experiments.anchoring.summary` | `experiments/results/anchoring/live`, `cadence`, `summary_clean.json`, `paper/tables/anchoring_*.tex` |

Two snapshots of the clean period are planned. The clean period starts when the v0.1.5 worker
started, on 10 November 2026 at 18:47:10 UTC (11 November, 00:17 IST):

- Snapshot A, on 18 November 2026: 7 clean days, an early look. It is a deviation from the
  specification, which asks for the rewrite window "as a distribution over at least 2 weeks of
  real runs", and will be reported as one.
- Snapshot B, on 25 November 2026 after 00:17 IST: 14 clean days. This is the primary snapshot
  and meets the SPEC.

Snapshot A was not taken. Snapshot B was taken on 24 November 2026 at 19:03:34 UTC (25 November,
00:33 IST), read only, through the SSH tunnel (`experiments/results/anchoring/live/snapshot.txt`).
The clean period runs from 10 November 18:47:10 UTC to the snapshot; no event was written after
the 14 day mark (`summary_clean.json`, `events_after_14_days`). The tables are in
`paper/tables/anchoring_*.tex`, the figure in `paper/figures/fig_rewrite_window.pdf`.

Clinic ids in the snapshot: `018b7266-7840-7319-9498-c18c799e670c` is Saathi Demo Clinic A and
`018b7266-7840-76c6-b6ad-fa7be7efed79` is Saathi Demo Clinic B (one read-only SELECT on the live
`clinics` table, 25 November 2026).

**Gas by period.** `anchoring_gas.tex` has one row per study period (by the time the anchor was
sent) and the Besu bench row. The 85,639 gas maximum of the earlier version of the table was each
clinic's first Amoy anchor (clinic A tree size 1,283, sent 31 October 16:54:59 UTC; clinic B tree
size 1,200, 16:55:27 UTC). Both were sent before the timer-working period starts (17:20 UTC), so
they are in no period and are left out of the table now, as the windows table leaves out events
outside every period. Why the first anchor costs more: `AuditAnchor` keeps one
`Head(uint64 treeSize, bytes32 root)` per clinic, two storage slots, and the first anchor writes
both from zero, which costs more than changing them later. The Besu bench shows the same step:
its first anchor used 75,439 gas and the later ones 41,587 to 41,635 (the "Bench run" row). The
gas difference was not traced unit by unit.

**Why Amoy lagged in the timer-working period** (Amoy P95 115.7 minutes, GitHub 14.7). Of the
172 checkpoints signed in that period, 15 have a GitHub receipt and no Amoy receipt (snapshot
`receipts.parquet`). They fall in two spans, both clinics at once:

- 2 November, 12:00 to 13:21 IST: no Amoy anchor between 11:29 and 13:36 IST for clinic A and
  between 11:45 and 13:37 IST for clinic B; GitHub published every 15 minute checkpoint.
- 4 November, 19:33 to 19:48 IST: no Amoy anchor between 19:17 and 20:03 IST for either clinic.

Events in those checkpoints wait for the next Amoy anchor. All 2,370 events of the period with an
Amoy window over 30 minutes were written in these two spans (2 November 11:45 to 13:29 IST, 1,643
events; 4 November 19:20 to 19:59 IST, 727 events). That is 11.2% of the period's 21,213 events,
more than the 5% above the P95, so these spans set the P95 and the 197 minute maximum. Every
Amoy anchor that was sent was mined within a second of being sent.

What is not proven is why the Amoy step failed in those spans. When `send_anchor` raises (for
example an RPC error or too little POL for gas), the job logs "anchor step failed" and writes no
receipt (`app/anchor/job.py` `anchor_on_chain`), so the database has no record of the attempts;
the worker logs of those days were lost at a later redeploy. The poster wallet's balance at those
blocks could not be read: the public Amoy RPC refuses balance queries at past blocks without a
personal token. The gas price of the anchors around both spans was a steady 30 gwei. The
"Anchoring fixes found on the live demo" entry in `docs/CHANGELOG.md` covers OpenTimestamps and
the worker fallback interval, not this.

## What remains

- Anchoring study: write it up in the paper (Snapshot B and its tables are committed).
- Paper: not started. A fuller literature search (Google Scholar and the ACM Digital Library) is
  needed before writing the related work and the novelty claim.
- Hosting after the Azure Free Trial ends (about 30 November 2026): move to Azure for Students,
  upgrade, or take the demo offline. This is the project owner's decision.
- Smaller items: a password strength meter and a 12-character
  minimum password (the specification currently says 10).

Designed to support obligations under the DPDP Act 2023 and DPDP Rules 2025. Not legally
reviewed or certified.

# ADR 0008: Features, scoring and alerts

Status: accepted (amended 13 November 2026: daily final list, starter rules)

## Context

SPEC 5.4 and 5.5 define the residual features, the gate, pluggable scorers, per-clinic fitting with
a global fallback and a daily alert budget. The SPEC does not say how features run on both the
database and the simulator, how a daily top-B works when accesses arrive one at a time, where
fitted models live, or what the product does before any model is fitted.

## Decision

**One feature builder over table-shaped frames.** `e2d_core.features.build_features` takes polars
frames with the product's table and column names. The simulator passes its Parquet tables; the
API loads one clinic's rows from Postgres into the same frames (`app/detect/loader.py`). Every
feature uses only rows created at or before the access, so the values are the same whether they
are computed live or replayed.

**Graph features.** Care edges come from appointments, encounters, referrals, care-team rows, lab
orders and results, and vitals. Because the care graph is bipartite (users and patients), a
user-to-patient path is 1 (direct), 3 (a colleague who shares a patient with the user cares for the
patient) or longer, capped at 4. "Jaccard of the user's patients with the patient's care team" is
computed between the user's colleagues and the patient's care team, so both sets hold users.

**Gate, scorers and budget in e2d-core.** `gate`, `rules_score`, `fit`, `fit_per_clinic`,
`calibrate` and `budget` live in `e2d_core.detect` and are shared by the product and the
experiments. The product's fallback scorer, the **starter rules**, adds points for fired rules to
one minus the explanation strength; its weights and threshold are design choices. It is not the
B0 rules baseline of the experiments (B0 uses raw behaviour only and lives in `experiments/`).
The rules themselves are still in `e2d_core.detect.RULES`, where a comment still calls them B0;
the app reaches them only through `app/detect/starter_rules.py` (see docs/APP_GAPS.md).

**Provisional alerts, then a daily final list.** Experiments rank a whole clinic-day at once
(`budget`), with forgery flags as of 24 hours after each access. The product does both:

- *Provisional.* As accesses happen, each fitted model has a threshold calibrated so that about B
  accesses a day score above it on its training data, and the worker raises at most B alerts per
  clinic and local day, highest score first, with the flags known at the access. These alerts
  carry `features.lifecycle.stage = "provisional"`.
- *Final* (`app/detect/daily.py`). Once every access of clinic day D is at least 24 hours old, the
  worker makes D's final list the way the experiments do: it recomputes the late flags of each
  staff access as of 24 hours after it, builds the features, fits a final model on the gated staff
  accesses of the 30 days before D (per clinic with at least 500 such rows, otherwise one over all
  clinics; the starter rules when there is nothing to fit on), and keeps the top B gated staff
  accesses with `budget`. Break-glass accesses never use the budget.
- The final list adds alerts for accesses that had none and marks unreviewed provisional alerts
  `final` or `retired`. It never changes an alert someone has reviewed: a reviewed alert outside
  the final list stays as it was and is shown as "reviewed, not in final list". The alert list
  hides retired alerts unless asked (`include_retired`).

**When the final list is made.** Day D is the clinic's local calendar day (`clinics.timezone`). An
access at 23:59:59 on D needs its flags as of 23:59:59 on D+1, so D becomes due at 00:00 clinic
time on D+2, by the database clock. The worker checks every `DETECT_INTERVAL_SECONDS` (60 s by
default). Delay from an access to its place in the final list, before the worker tick and the
job's own run time:

| Access time on day D (clinic time) | Delay |
|---|---|
| 23:59:59 (best case) | 24 h |
| 20:00, end of the evening OPD session | 28 h |
| 09:30, start of the morning OPD session | 38.5 h |
| 00:00:00 (worst case) | 48 h |

The paper times every E2D alert 24 hours after its access; the product's worst case is 48 hours,
because it ranks a whole day at once. Accepted by the authors on 13 November 2026.

**Rerun safety.** A session advisory lock lets one daily run go ahead at a time. Each clinic day
is finalised in one transaction that writes the alert changes and one audit event of kind
`detect_daily` (day, as-of rule, model version, training window, counts and ids of added,
finalised and retired alerts, accesses whose late flags stayed undecided, cache sha256). That audit event is what marks
the day done, so a crash before the commit leaves the day to be redone, and a finalised day is
never computed again. The worker finalises missed days oldest first, up to
`DETECT_CATCH_UP_DAYS` days back (7 by default); older unfinished days get a `detect_daily`
event with outcome `skipped`. The first run on a database with history is done by hand with a
wider window (`python -m app.detect.job --daily --catch-up N`, docs/RUNBOOK.md), and the demo
seed finalises every loaded day that is due.

**Work per worker tick.** Per-minute scoring runs in the same worker loop as the daily job, just
before it, and the anchor timer shares the worker's event loop, which the daily job's CPU-bound
steps (features, explanations, fitting) hold. So each tick finalises at most one clinic day with
accesses and computes at most 3 days of training features; a day whose model needs more waits
for later ticks. Gate, ranking and budget are unchanged by the caps: they decide when a day is
finalised, not what its final list is.

**Break-glass.** `gate()` lets break-glass accesses through (they are always of interest), and
`budget()` leaves them out of the ranking, so on the daily path too they never enter the final
list or use a budget slot (`test_break_glass_never_enters_the_final_list_or_uses_the_budget`).

**Late flags.** The job keeps the stored explanation (template, strength, evidence) and
computes only the two late flags (`no_progress`, `cancelled_after_access`), with e2d-core's
`forgery_flags`, for the evidence that stored explanation names: the appointment, lab order,
referral, care team row or invoice that explained the access when it happened, as of 24 hours
after the access. The template is not chosen again. Each access gets one late-flag status:
`decided` (computed for the named record), `not_applicable` (its evidence is a queue token, a
registration, a typed reason or break-glass, or it has no explanation; both flags null, as the
engine gives them), `undecided` (the named record cannot be found; both flags null, and the
access id is listed in the audit event) or `denied` (a denied access stored without an
explanation). The daily audit event carries the count of each status. To find the record the job calls the private
`e2d_core.explain.forgery._find` (docs/APP_GAPS.md).

An earlier version of this job, never released, re-explained each access and kept its late
flags only when the re-explanation picked the same template and evidence. On days recorded
through the API it lost the late flags of more than 1% of staff accesses, because a
re-explanation sees later rows (a token assigned to the nurse after her access, an appointment
created in the same request) that the access itself could not. Computing the flags for the
stored evidence removes that; `apps/api/tests/detect/test_daily_lifecycle.py` requires undecided
late flags on at most 1% of the accesses where the flags apply, with and without the nurse
assignment, and prints the share of all staff accesses too.

**Feature cache.** The features of finalised days are kept per clinic in
`MODEL_DIR/daily/<clinic>.<key>.parquet` (access event ids, days, late-flag status and feature
values; no names or clinical text), so the 30-day training window is not recomputed every night.
It is never the source of truth: a missing or unreadable file is rebuilt from the database and
the rebuild is logged. Its sha256 is in each daily audit event.

**Models are files.** The nightly fit of the provisional models writes one file per clinic with at
least 500 gated accesses in the last 30 days, and a global file for the rest, to MODEL_DIR. The
file's sha256 is the `model_version` stored on every provisional alert. Before the first fit the
starter rules are used, and their version is the sha256 of the rules. A final model is fitted for
each finalised day and not kept; the sha256 of its serialised form is the `model_version` of every
final alert of that day and is in the daily audit event. No table is added for models.

**Stored explanations.** The per-minute scorer uses the explanation and forgery flags stored when
the access happened, so the late flags are undecided in provisional alerts. The daily job
recomputes them as described above and stores them on final alerts under
`features.late_flags`; `access_explanations` stays insert-only and is never updated.

**Review.** Opening an alert shows the patient, so it is recorded through `record_and_explain`. The
user timeline shows other patients only as short pseudonyms. Nobody can review an alert about their
own access, and every review is written to the audit log with the model version.

## Consequences

- The final list matches the experiments' E2D ranking: `infra/demo/tests/test_daily_parity.py`
  ranks the same SaathiBench clinic days through both paths with one fitted model and checks
  that the top-B sets (and their order) are equal.
- The model training window differs from the experiments: the registered runs fit once on
  days 1 to 120 and score days 121 to 180; the product fits a new model for each day on the 30
  days before it. See the deviation of 13 November 2026 in docs/CHANGELOG.md.

- New registrations used to fill the alert list: when reception registers a patient and then
  opens the record, the registration counted as evidence the same user created within the hour
  (`self_created_recent`). The authors decided that a registration never counts for this one flag;
  every other indicator still applies to it (SPEC 5.3).
- Scoring reloads one clinic's last week of accesses each minute. This suits small clinics; a larger
  deployment would score incrementally.

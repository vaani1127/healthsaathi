# ADR 0008: Features, scoring and alerts

Status: accepted

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
experiments. The rules baseline (B0) adds points for fired rules to one minus the explanation
strength; its weights and threshold are design choices.

**Online budget.** Experiments rank a whole clinic-day at once (`budget`). The product sees accesses
as they happen, so each fitted model gets a threshold calibrated so that about B accesses a day
score above it on its training data, and the worker raises at most B alerts per clinic and local
day, highest score first. Break-glass accesses never use the budget.

**Models are files.** The nightly fit writes one file per clinic with at least 500 gated accesses in
the last 30 days, and a global file for the rest, to MODEL_DIR. The file's sha256 is the
`model_version` stored on every alert. Before the first fit the rules baseline is used, and its
version is the sha256 of its rules. No table is added for models.

**Stored explanations.** The worker uses the explanation and forgery flags stored when the access
happened. Flags that need later information (no progress, cancelled after access) are therefore
undecided in the product; the experiments can recompute them with a later `as_of`.

**Review.** Opening an alert shows the patient, so it is recorded through `record_and_explain`. The
user timeline shows other patients only as short pseudonyms. Nobody can review an alert about their
own access, and every review is written to the audit log with the model version.

## Consequences

- The rules baseline flags new registrations: when reception registers a patient and then opens the
  record, the registration counts as evidence the same user created within the hour
  (`self_created_recent`, SPEC 5.3). Fitted models learn that this is common; the rules baseline
  does not.
- Scoring reloads one clinic's last week of accesses each minute. This suits small clinics; a larger
  deployment would score incrementally.

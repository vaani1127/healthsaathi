# ADR 0007: SaathiBench simulator, attacks and audit

Status: accepted

## Context

SPEC 7 asks for a simulator that writes the product's table shapes, benign hard negatives, ten
attack types with a mimicry knob, labels that feature code cannot read, a separability audit and
four splits. Several details were open: how state that changes over time is written, what mimicry
means for each attack, which features the audit uses, and how splits are drawn.

## Decision

**One SimPy model per clinic, run in parallel.** Each clinic has its own random generator seeded
from the run seed and the clinic's position, so output does not depend on the number of worker
processes. Rows are written as Parquet parts per clinic.

**Final snapshot of changing rows.** Appointments, tokens, lab orders and invoices are written once,
in their final state with `updated_at` set to the last change, as a later database dump would show
them. Referrals stay `active` until `valid_until`, so referral evidence is not hidden by a later
status change.

**The product's policy file decides.** The simulator reads `apps/api/app/access/policy.yaml`, so
refused accesses and `policy_version` are exactly the product's. A test checks the two readers
agree.

**Attacks live inside the workflow.** Attack accesses are written through the same code path as
benign ones, and whatever an attacker creates (appointments, lab orders, care-team rows,
break-glass events, note versions, devices) goes into the normal tables. Only `labels/` tells them
apart.

**Mimicry.** For a campaign with level m, each attack time is
`lerp(off_hours_time, actor_median_time, m)` plus noise, where the actor's median time comes from
their own benign accesses so far. Volumes and burst speeds are interpolated between an m = 0 value
and an m = 1 value (fewer patients, spread over more days, slower). For forgery, a careful attacker
creates the evidence hours rather than minutes before using it.

**Labels are walled off.** Labels are written only under `<run>/labels/`, read only through
`saathibench.labels`, and import-linter forbids e2d-core, the product and the generator from
importing that module.

**Audit features exclude explanations.** The separability audit uses the e2d-core behaviour
features plus hour, weekday and refusal. Using explanation strength there would let the generator
be tuned against E2D, which SPEC 7 forbids.

**Splits.** Two thirds train in each split. Clinic split quotas per profile use largest remainder,
so small configs still get both sides. The attack split drops types 6 and 10 from training only.

**Faster in-memory evidence.** e2d-core's in-memory repository builds per-patient and per-user
indexes once and answers each lookup with binary searches instead of scanning frames, so coverage
and experiments can explain millions of accesses. The shared backend tests and a direct comparison
on simulator output show the same bundles.

## Consequences

- There is no history before the first simulated day; templates that need earlier visits are
  rarer at first, and attacks that need history wait for it.
- Changing the generator changes the benchmark: a new release needs a new seed or version, a fresh
  audit and an updated data card.

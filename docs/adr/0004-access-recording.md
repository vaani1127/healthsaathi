# ADR 0004: Recording and explaining accesses

Status: accepted

## Context

SPEC 4 and 5 require every read or write of patient data to produce an access event, its
explanation and an audit event in the same transaction as the read, using one explanation engine
shared with the experiments.

## Decision

**One entry point.** Services call `access.record_and_explain()` with the request's database
session before returning or changing patient data. It checks the deterministic policy
(`apps/api/app/access/policy.yaml`), explains the access with `e2d_core.explain.explain()` over
evidence read by `e2d_core.repo.sql.SqlRepository` in the same session, and writes the three rows.
List screens call `record_many()`, which writes one access event per patient shown.

**Patients only see themselves.** The policy lets the patient role read its own record; the access
service also checks `patients.user_id` and denies anything else.

**Denials are always logged.** A denial raises `AccessDeniedError`. The request dependency
(`access_session`) catches it after the request's transaction has rolled back and writes the
denial in a new transaction. Writing it inside the failed transaction would lose it, and writing it
in a parallel transaction could deadlock on the audit chain lock.

**Database clock.** The access time is `clock_timestamp()` from Postgres, the same clock that sets
`created_at` on workflow rows. With the application clock, a few milliseconds of skew made evidence
created in the same request look newer than the access, so it was ignored.

**What counts as which action.** Registering a patient is a `create` on demographics. Booking an
appointment, issuing a walk-in or moving a queue token shows who the patient is but does not change
their record, so it is a `view` on demographics. Changing appointment status or patient details is
an `edit`.

**Two repository backends.** `SqlRepository` (Postgres, async) and `MemoryRepository` (polars
frames with the same table and column names) implement `EvidenceRepository`. One test suite runs
against both and also checks that they return identical bundles.

**Realtime.** Services call `realtime.hub.notify()` inside their transaction. Postgres delivers
`NOTIFY` only on commit, every API worker listens and forwards to its WebSocket clients, and
messages carry ids and types only. Clients refetch through the API, which records the access.

## Consequences

- A list of 40 patients writes 40 access events. That is the intended granularity for detection.
- Evidence is fetched with a few small queries per access; this is fine at clinic scale and will be
  measured in the overhead study (SPEC 8, RQ5) rather than assumed.

# ADR 0002: Database roles, row level security and partitioning

Status: accepted

## Context

SPEC section 3 asks for row level security on every operational table, an append-only audit log,
monthly partitions for `access_events` and `audit_events`, and four database roles. These choices
fill in the details SPEC leaves open.

## Decision

**Ownership.** The first migration creates the roles `migrator`, `app_rw`, `anchor_job` and
`researcher_ro` (all `NOLOGIN`, none with `BYPASSRLS`), then runs `SET ROLE migrator` so every table,
function and partition is owned by `migrator`. The user running Alembic needs `CREATEROLE` the first
time.

**The app always runs as `app_rw`.** Every application transaction starts with
`SET LOCAL ROLE app_rw` and sets `app.clinic_id`, `app.user_id` and `app.role` with
`set_config(..., true)`. This happens in a SQLAlchemy `after_begin` hook, so it is applied to every
transaction, including ones started after a commit. Because of this, RLS applies even if
`DATABASE_URL` logs in as a more powerful user, and `FORCE ROW LEVEL SECURITY` is not needed.

**Policies.** Every table with `clinic_id` has a policy for `app_rw`:
`clinic_id = app_clinic_id()` for both `USING` and `WITH CHECK`. With no tenant context set,
`app_clinic_id()` is NULL and no rows match. `memberships` also lets a user read their own rows
before choosing a clinic. `clinics` is readable by `app_rw`; only `platform_admin` can insert.
`anchor_job` has its own `USING (true)` policies on `audit_events`, `merkle_checkpoints`,
`anchor_receipts` and `clinics`, so it can work across clinics without `BYPASSRLS`.

**Same-clinic references.** Every clinic scoped table has a unique key on `(clinic_id, id)`, and
foreign keys between clinic scoped tables are composite: `(clinic_id, patient_id)` references
`patients (clinic_id, id)`. A row in clinic A can never point at a patient of clinic B, even though
foreign key checks themselves ignore RLS.

**Identity tables have no RLS.** `users`, `devices`, `sessions`, `mfa_secrets`, `email_otps`,
`push_subscriptions`, `jobs` and `policy_versions` have no `clinic_id`. Login and token refresh must
read them before any clinic is known, so access to them is controlled by the identity service.

**Append-only.** `app_rw` has `SELECT` and `INSERT` but no `UPDATE` or `DELETE` on `audit_events`,
`access_events` and `access_explanations`. A trigger also rejects `UPDATE`, `DELETE` and `TRUNCATE`
on those tables for every role, including the owner. The tamper demo in P10 has to disable the
trigger as a superuser, which is the point.

**Partitions.** `create_month_partitions(parent, months_back, months_ahead)` is a `SECURITY DEFINER`
function owned by `migrator`. The migration creates 3 months back and 6 ahead. Partitions get no
grants, so they can only be read through the parent table, where the policies apply. The function
is safe to call repeatedly and will be called on a schedule by the background worker.

**No foreign keys into partitioned tables.** Postgres can only reference a partitioned table through
its full primary key, which includes `at`. `access_explanations`, `alerts` and
`patient_access_queries` store `access_event_id` without a foreign key; the access service writes
them in the same transaction as the event.

**Researcher view.** `research.access_events_deid` exposes access events with SHA-256 pseudonyms
instead of user and patient ids, and no IP, session or device data. `researcher_ro` can read only
this view.

## Consequences

- A bug that forgets the tenant context returns no rows instead of another clinic's rows.
- Every new clinic scoped table must be added to the policy list in a migration; the RLS test suite
  checks that the number of clinic scoped tables matches, so a missed table fails the build.
- UUIDv7 ids are generated in Python (`e2d_core.ids.uuid7`) because Postgres 16 has no built-in
  generator.

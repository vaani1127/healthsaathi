# ADR 0005: Explanation templates, forgery flags, reasons and break-glass

Status: accepted

## Context

SPEC 5.2 and 5.3 define eleven explanation templates and six forgery indicators. Some details are
left open: how a decayed match is cut off, how flags that need the future are handled, and where a
doctor's typed reason and the emergency view live in the existing data model.

## Decision

**All parameters in one file.** Weights, windows, decay constants, the gate threshold theta and
the forgery thresholds are in `packages/e2d-core/src/e2d_core/explain/templates.yaml`. They are
design choices of the method, not measured results.

**Minimum strength.** A match weaker than `gate.min_strength` (0.001) is dropped, so evidence that
has decayed to almost nothing does not count as an explanation.

**Flags can be undecided.** Each flag is True, False or None. `no_progress` and
`cancelled_after_access` depend on what happens after the access, so at access time they are None;
`explain(..., as_of=...)` decides them later (nightly rescoring, P14). `off_path_creation` is None
until the clinic has at least 20 bookings in the last 30 days, and `created_off_hours` is None for a
clinic with no schedule. `any_flag()` only counts flags that are True.

**Flags describe the chosen evidence.** They are computed for the evidence behind the strongest
explanation, and only for evidence a user can create (appointments, lab orders, referrals, care team
assignments, invoices, registrations). Shifts, reasons, break-glass and self access are not flagged.
A registration never sets `self_created_recent` (registering and then opening the record is the
normal front-desk path); its other flags are computed as usual. Added in P15 by the authors'
decision.

**Typed reasons.** A doctor whose best explanation is below theta (0.5) gets HTTP 428 from
`GET /patients/{id}/chart`; the attempt is logged as a denial. `POST /patients/{id}/chart` with a
reason code and text opens the chart; every access in it is explained by T_REASON (0.3) and the
reason is stored in `access_explanations.evidence`, so no new column is needed.

**Break-glass.** `POST /patients/{id}/break-glass` (doctor or nurse) writes a `break_glass_events`
row and an audit event, then returns the emergency view: active allergies, active conditions,
medicines from the latest signed prescription and the last vitals. The break-glass id is valid for
4 hours and only for the same user and patient. Every row with no review is in the admin queue
(`GET /break-glass`), and a reviewer cannot review their own break-glass access.

**Conditions are under `notes`.** The access log has no separate resource type for conditions, so
reading the problem list is recorded as `notes`.

## Consequences

- A doctor sees the reason prompt for patients they have no current workflow link with, including
  a day or two after a visit once the appointment window has decayed below theta.
- Forgery flags stored at access time are partial; the stored record is the access-time view, and
  the scorer recomputes the full set later.

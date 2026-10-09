"""Explanation engine (SPEC 5.2).

Each template looks for workflow evidence that makes an access expected. The strength of a match is
weight * exp(-dt / tau), where dt is how far the access falls outside the template's core window.
The explanation is the strongest match, plus the forgery indicators of SPEC 5.3 for its evidence.
Evidence created after the access never counts, and a record's status is the one it had at the
access (from its status history), never a later one.
"""

import math
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from e2d_core.explain.forgery import forgery_flags
from e2d_core.explain.model import (
    AccessEvent,
    Candidate,
    EvidenceBundle,
    EvidenceRef,
    Explanation,
    InvoiceEv,
)
from e2d_core.explain.templates import TemplateConfig, TemplateSpec, default_config

MAX_ALTERNATIVES = 3

# Evidence stamped up to this long after the access still counts. In the product, evidence the
# engine can see already existed when the access happened; the check matters when replaying
# history, and the margin absorbs small backward steps of the database clock.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)

Matcher = Callable[[AccessEvent, EvidenceBundle, TemplateSpec, ZoneInfo], Iterator[Candidate]]


def window_distance(at: datetime, start: datetime, end: datetime | None) -> timedelta:
    """Zero inside [start, end] (end None means open), otherwise the gap to the nearest edge."""
    if at < start:
        return start - at
    if end is not None and at > end:
        return at - end
    return timedelta(0)


def decayed(spec: TemplateSpec, distance: timedelta) -> float:
    if distance <= timedelta(0):
        return spec.weight
    return spec.weight * math.exp(-distance / spec.tau)


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def existed(created_at: datetime | None, at: datetime) -> bool:
    return created_at is None or created_at <= at + CLOCK_SKEW_TOLERANCE


def match_appt(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for appt in bundle.appointments:
        if appt.doctor_user_id != event.user_id:
            continue
        if bundle.status_at("appointment", appt, event.at) == "cancelled":
            continue
        if appt.patient_id != event.patient_id or not existed(appt.created_at, event.at):
            continue
        dt = window_distance(event.at, appt.slot_start - spec.before, appt.slot_end + spec.after)
        yield Candidate(
            spec.code,
            decayed(spec, dt),
            (EvidenceRef("appointment", appt.id),),
            appt.slot_start,
            appt.created_by,
            appt.created_at,
        )


def match_queue(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    on_shift = [
        s
        for s in bundle.shifts
        if s.user_id == event.user_id
        and s.status != "cancelled"
        and s.starts_at <= event.at <= s.ends_at
    ]
    for token in bundle.queue_tokens:
        if token.patient_id != event.patient_id or token.status == "skipped":
            continue
        if not existed(token.created_at, event.at):
            continue
        # SPEC 5.2: the token is assigned to this nurse, or this nurse is on shift.
        refs = [EvidenceRef("queue_token", token.id)]
        if token.assigned_nurse_user_id != event.user_id:
            if not on_shift:
                continue
            refs.append(EvidenceRef("shift", on_shift[0].id))
        start, end = local_day_bounds(token.day, tz)
        dt = window_distance(event.at, start, end - timedelta(microseconds=1))
        yield Candidate(
            spec.code, decayed(spec, dt), tuple(refs), start, token.created_by, token.created_at
        )


def match_frontdesk(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    def around(anchor: datetime) -> timedelta:
        return window_distance(event.at, anchor - spec.before, anchor + spec.after)

    for appt in bundle.appointments:
        if appt.patient_id != event.patient_id or not existed(appt.created_at, event.at):
            continue
        dt = min(around(appt.slot_start), around(appt.created_at))
        yield Candidate(
            spec.code,
            decayed(spec, dt),
            (EvidenceRef("appointment", appt.id),),
            appt.slot_start,
            appt.created_by,
            appt.created_at,
        )
    for invoice in bundle.invoices:
        if invoice.patient_id != event.patient_id or not existed(invoice.created_at, event.at):
            continue
        yield Candidate(
            spec.code,
            decayed(spec, around(invoice.created_at)),
            (EvidenceRef("invoice", invoice.id),),
            invoice.created_at,
            invoice.created_by,
            invoice.created_at,
        )
    patient = bundle.patient
    if (
        patient is not None
        and patient.id == event.patient_id
        and existed(patient.created_at, event.at)
    ):
        yield Candidate(
            spec.code,
            decayed(spec, around(patient.created_at)),
            (EvidenceRef("registration", patient.id),),
            patient.created_at,
            patient.created_by,
            patient.created_at,
        )


def match_lab(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for order in bundle.lab_orders:
        if order.patient_id != event.patient_id:
            continue
        if bundle.status_at("lab_order", order, event.at) == "cancelled":
            continue
        if not existed(order.created_at, event.at):
            continue
        end = order.released_at + spec.after if order.released_at else None
        yield Candidate(
            spec.code,
            decayed(spec, window_distance(event.at, order.created_at, end)),
            (EvidenceRef("lab_order", order.id),),
            order.created_at,
            order.ordered_by,
            order.created_at,
        )


def match_referral(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for ref in bundle.referrals:
        if ref.to_user_id != event.user_id or ref.patient_id != event.patient_id:
            continue
        if not existed(ref.created_at, event.at):
            continue
        if bundle.status_at("referral", ref, event.at) != "active":
            continue
        yield Candidate(
            spec.code,
            decayed(spec, window_distance(event.at, ref.created_at, ref.valid_until)),
            (EvidenceRef("referral", ref.id),),
            ref.created_at,
            ref.created_by,
            ref.created_at,
        )


def match_careteam(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for member in bundle.care_team:
        if member.user_id != event.user_id or member.patient_id != event.patient_id:
            continue
        if not existed(member.created_at, event.at):
            continue
        yield Candidate(
            spec.code,
            decayed(spec, window_distance(event.at, member.starts_at, member.ends_at)),
            (EvidenceRef("care_team", member.id),),
            member.starts_at,
            member.created_by,
            member.created_at,
        )


def match_followup(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    seen: list[tuple[datetime, EvidenceRef]] = [
        (a.slot_start, EvidenceRef("appointment", a.id))
        for a in bundle.appointments
        if a.doctor_user_id == event.user_id
        and a.patient_id == event.patient_id
        and a.slot_start <= event.at
        and bundle.status_at("appointment", a, event.at) == "completed"
    ]
    seen += [
        (e.started_at, EvidenceRef("encounter", e.id))
        for e in bundle.encounters
        if e.doctor_user_id == event.user_id
        and e.patient_id == event.patient_id
        and e.started_at <= event.at
    ]
    upcoming = [
        a
        for a in bundle.appointments
        if a.doctor_user_id == event.user_id
        and a.patient_id == event.patient_id
        and a.slot_start > event.at
        and existed(a.created_at, event.at)
        and bundle.status_at("appointment", a, event.at) not in ("cancelled", "no_show")
    ]
    if not seen or not upcoming:
        return
    last_seen, seen_ref = max(seen, key=lambda s: s[0])
    future = min(upcoming, key=lambda a: a.slot_start)
    dt = window_distance(last_seen, event.at - spec.lookback, event.at)
    yield Candidate(
        spec.code,
        decayed(spec, dt),
        (seen_ref, EvidenceRef("appointment", future.id)),
        last_seen,
        future.created_by,
        future.created_at,
    )


def _paid_at(bundle: EvidenceBundle, invoice: InvoiceEv) -> datetime:
    """When the invoice was paid, from its status history (its last update if it has none)."""
    paid = [s.at for s in bundle.status_changes("invoice", invoice) if s.status == "paid"]
    return paid[0] if paid else invoice.updated_at


def match_billing(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for invoice in bundle.invoices:
        if invoice.patient_id != event.patient_id or not existed(invoice.created_at, event.at):
            continue
        status = bundle.status_at("invoice", invoice, event.at)
        if status == "void":
            continue
        end = _paid_at(bundle, invoice) + spec.after if status == "paid" else None
        yield Candidate(
            spec.code,
            decayed(spec, window_distance(event.at, invoice.created_at, end)),
            (EvidenceRef("invoice", invoice.id),),
            invoice.created_at,
            invoice.created_by,
            invoice.created_at,
        )


def match_reason(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    if event.reason is not None and event.reason.text.strip():
        yield Candidate(
            spec.code, spec.weight, (EvidenceRef("reason", event.id),), event.at, event.user_id
        )


def match_breakglass(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for bg in bundle.break_glass:
        if bg.user_id != event.user_id or bg.patient_id != event.patient_id:
            continue
        if not existed(bg.at, event.at):
            continue
        yield Candidate(
            spec.code,
            decayed(spec, window_distance(event.at, bg.at, bg.at + spec.after)),
            (EvidenceRef("break_glass", bg.id),),
            bg.at,
            bg.user_id,
            bg.at,
        )


def match_self(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    patient = bundle.patient
    if (
        patient is not None
        and patient.id == event.patient_id
        and patient.user_id is not None
        and patient.user_id == event.user_id
    ):
        yield Candidate(spec.code, spec.weight, (EvidenceRef("patient", patient.id),))


MATCHERS: dict[str, Matcher] = {
    "T_APPT": match_appt,
    "T_QUEUE": match_queue,
    "T_FRONTDESK": match_frontdesk,
    "T_LAB": match_lab,
    "T_REFERRAL": match_referral,
    "T_CARETEAM": match_careteam,
    "T_FOLLOWUP": match_followup,
    "T_BILLING": match_billing,
    "T_REASON": match_reason,
    "T_BREAKGLASS": match_breakglass,
    "T_SELF": match_self,
}


def candidates(
    event: AccessEvent, bundle: EvidenceBundle, config: TemplateConfig, tz: ZoneInfo
) -> list[Candidate]:
    found: list[Candidate] = []
    for code, spec in config.templates.items():
        matcher = MATCHERS.get(code)
        if matcher is None or not spec.applies_to(event.role, event.resource):
            continue
        found.extend(
            c for c in matcher(event, bundle, spec, tz) if c.strength >= config.min_strength
        )
    # Strongest first; ties go to the evidence closest in time to the access.
    found.sort(
        key=lambda c: (
            -c.strength,
            abs((c.anchor_time - event.at).total_seconds()) if c.anchor_time else 0.0,
        )
    )
    return found


def explain(
    event: AccessEvent,
    bundle: EvidenceBundle,
    config: TemplateConfig | None = None,
    timezone: str = "Asia/Kolkata",
    as_of: datetime | None = None,
) -> Explanation:
    """Explain one access. `as_of` is when the bundle was observed (default: the access time);
    flags that need later information (no_progress, cancelled_after_access) stay None until then.
    """
    config = config or default_config()
    tz = ZoneInfo(timezone)
    found = candidates(event, bundle, config, tz)
    best = found[0] if found else None
    flags = forgery_flags(event, best, bundle, config.forgery, tz, as_of or event.at)
    if best is None:
        return Explanation(template_code=None, strength=0.0, forgery_flags=flags)
    return Explanation(
        template_code=best.template,
        strength=best.strength,
        evidence=best.evidence,
        alternatives=tuple(found[1 : 1 + MAX_ALTERNATIVES]),
        forgery_flags=flags,
        reason=event.reason if best.template == "T_REASON" else None,
    )

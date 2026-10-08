"""Explanation engine (SPEC 5.2).

Each template looks for workflow evidence that makes an access expected. The strength of a match is
weight * exp(-dt / tau), where dt is how far the access falls outside the template's core window.
The explanation is the strongest match. Evidence created after the access never counts.
"""

import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from e2d_core.explain.model import (
    AccessEvent,
    Candidate,
    EvidenceBundle,
    EvidenceRef,
    Explanation,
)
from e2d_core.explain.templates import TemplateConfig, TemplateSpec, default_config

MAX_ALTERNATIVES = 3

Matcher = Callable[[AccessEvent, EvidenceBundle, TemplateSpec, ZoneInfo], Iterator[Candidate]]


def window_distance(at: datetime, start: datetime, end: datetime) -> timedelta:
    """Zero inside [start, end], otherwise the gap to the nearest edge."""
    if at < start:
        return start - at
    if at > end:
        return at - end
    return timedelta(0)


def decayed(spec: TemplateSpec, distance: timedelta) -> float:
    if distance <= timedelta(0):
        return spec.weight
    return spec.weight * math.exp(-distance / spec.tau)


def local_day_bounds(day: object, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)  # type: ignore[arg-type]
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _existed(created_at: datetime | None, at: datetime) -> bool:
    return created_at is None or created_at <= at


def match_appt(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    for appt in bundle.appointments:
        if appt.doctor_user_id != event.user_id or appt.status == "cancelled":
            continue
        if appt.patient_id != event.patient_id or not _existed(appt.created_at, event.at):
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
        if not _existed(token.created_at, event.at):
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
            spec.code,
            decayed(spec, dt),
            tuple(refs),
            start,
            token.created_by,
            token.created_at,
        )


def match_frontdesk(
    event: AccessEvent, bundle: EvidenceBundle, spec: TemplateSpec, tz: ZoneInfo
) -> Iterator[Candidate]:
    def around(anchor: datetime) -> timedelta:
        return window_distance(event.at, anchor - spec.before, anchor + spec.after)

    for appt in bundle.appointments:
        if appt.patient_id != event.patient_id or not _existed(appt.created_at, event.at):
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
        if invoice.patient_id != event.patient_id or not _existed(invoice.created_at, event.at):
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
        and _existed(patient.created_at, event.at)
    ):
        yield Candidate(
            spec.code,
            decayed(spec, around(patient.created_at)),
            (EvidenceRef("registration", patient.id),),
            patient.created_at,
            patient.created_by,
            patient.created_at,
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
    "T_SELF": match_self,
}


def explain(
    event: AccessEvent,
    bundle: EvidenceBundle,
    config: TemplateConfig | None = None,
    timezone: str = "Asia/Kolkata",
) -> Explanation:
    config = config or default_config()
    tz = ZoneInfo(timezone)
    candidates: list[Candidate] = []
    for code, spec in config.templates.items():
        matcher = MATCHERS.get(code)
        if matcher is None or not spec.applies_to(event.role, event.resource):
            continue
        candidates.extend(c for c in matcher(event, bundle, spec, tz) if c.strength > 0)

    if not candidates:
        return Explanation(template_code=None, strength=0.0)
    # Strongest first; ties go to the evidence closest in time to the access.
    candidates.sort(
        key=lambda c: (
            -c.strength,
            abs((c.anchor_time - event.at).total_seconds()) if c.anchor_time else 0.0,
        )
    )
    best = candidates[0]
    return Explanation(
        template_code=best.template,
        strength=best.strength,
        evidence=best.evidence,
        alternatives=tuple(candidates[1 : 1 + MAX_ALTERNATIVES]),
    )

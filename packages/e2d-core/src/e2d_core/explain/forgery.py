"""Forgery indicators F(a) (SPEC 5.3).

They describe the evidence behind the chosen explanation: was it made by the same user just before
the access, did the care it implies ever happen, was it made outside the usual path or hours, was
it cancelled afterwards, and does this user create unusually much evidence.

Flags are True, False, or None when they cannot be decided yet (for example no_progress before 24
hours have passed) or do not apply to the evidence.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from e2d_core.explain.model import AccessEvent, Candidate, EvidenceBundle
from e2d_core.explain.templates import ForgeryConfig

FLAG_NAMES = (
    "self_created_recent",
    "no_progress",
    "off_path_creation",
    "created_off_hours",
    "cancelled_after_access",
    "self_creation_high",
)

# Evidence that a user can create themselves, so forging it is possible.
CREATABLE = {"appointment", "lab_order", "referral", "care_team", "invoice", "registration"}
# Evidence that implies care should follow.
NEEDS_PROGRESS = {"appointment", "lab_order"}
CANCELLED = {"cancelled", "no_show", "void"}
CLOCK_SKEW = timedelta(seconds=5)


def _find(bundle: EvidenceBundle, kind: str, ident: object) -> Any:
    pools: dict[str, tuple[Any, ...]] = {
        "appointment": bundle.appointments,
        "lab_order": bundle.lab_orders,
        "referral": bundle.referrals,
        "care_team": bundle.care_team,
        "invoice": bundle.invoices,
    }
    for item in pools.get(kind, ()):
        if item.id == ident:
            return item
    return None


def _off_hours(created_at: datetime, bundle: EvidenceBundle, tz: ZoneInfo) -> bool | None:
    if not bundle.clinic.hours:
        return None
    local = created_at.astimezone(tz)
    return not any(
        h.weekday == local.weekday() and h.start <= local.time() < h.end
        for h in bundle.clinic.hours
    )


def self_creation_rate(bundle: EvidenceBundle) -> float:
    return (bundle.user.created_7d + 1) / (bundle.user.role_median_7d + 1)


def forgery_flags(
    event: AccessEvent,
    best: Candidate | None,
    bundle: EvidenceBundle,
    config: ForgeryConfig,
    tz: ZoneInfo,
    as_of: datetime,
) -> dict[str, Any]:
    rate = self_creation_rate(bundle)
    flags: dict[str, Any] = {
        "self_created_recent": None,
        "no_progress": None,
        "off_path_creation": None,
        "created_off_hours": None,
        "cancelled_after_access": None,
        "self_creation_rate": round(rate, 4),
        "self_creation_high": rate >= config.self_creation_high and bundle.user.created_7d >= 3,
    }
    if best is None or not best.evidence:
        return flags
    ref = best.evidence[-1] if best.template == "T_FOLLOWUP" else best.evidence[0]
    if ref.kind not in CREATABLE or best.created_at is None:
        return flags

    created_at = best.created_at
    age = event.at - created_at
    flags["self_created_recent"] = (
        best.created_by == event.user_id and -CLOCK_SKEW <= age <= config.self_created_recent
    )
    flags["created_off_hours"] = _off_hours(created_at, bundle, tz)

    item = _find(bundle, ref.kind, ref.id)
    if ref.kind == "appointment" and item is not None:
        norm = bundle.clinic.reception_booking_share
        flags["off_path_creation"] = (
            None if norm is None else item.source != "reception" and norm >= config.reception_norm
        )
    elif ref.kind in ("lab_order", "referral", "care_team", "invoice"):
        flags["off_path_creation"] = False

    if ref.kind in NEEDS_PROGRESS:
        deadline = created_at + config.no_progress_after
        if as_of >= deadline:
            flags["no_progress"] = not any(created_at < p.at <= deadline for p in bundle.progress)

    if item is not None and as_of > event.at:
        updated = getattr(item, "updated_at", None)
        flags["cancelled_after_access"] = bool(
            item.status in CANCELLED and updated is not None and updated > event.at
        )
    return flags


def any_flag(flags: dict[str, Any]) -> bool:
    """True when any boolean indicator fired (used by the scoring gate, SPEC 5.5)."""
    return any(flags.get(name) is True for name in FLAG_NAMES)

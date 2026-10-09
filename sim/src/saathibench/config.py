"""Run configuration and clinic profiles (YAML). See sim/configs and sim/profiles."""

from dataclasses import dataclass, field, replace
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml

ROLES = ("doctor", "nurse", "reception", "lab_tech", "clinic_admin")
# Reception marks a patient who never came as a no-show this many minutes after the slot.
NO_SHOW_MARKED_AFTER = 120


@dataclass(frozen=True)
class Session:
    start: time
    end: time


@dataclass(frozen=True)
class HardNegatives:
    after_hours_emergencies_per_month: float = 0.0
    nurse_lab_followup_rate: float = 0.0
    pharmacy_checks_per_day: float = 0.0
    month_end_billing_share: float = 0.0
    staff_self_view_per_month: float = 0.0


@dataclass(frozen=True)
class Profile:
    name: str
    staff: dict[str, int]
    weekday: tuple[Session, ...]
    saturday: tuple[Session, ...]
    sunday: tuple[Session, ...]
    slot_minutes: int
    visits_per_doctor_session: float
    walkin_share: float
    no_show_rate: float
    cancel_rate: float
    initial_patients: int
    new_patient_share: float
    chronic_share: float
    anc_share: float
    portal_share: float
    lab_order_rate: float
    referral_rate: float
    acute_followup_rate: float
    health_camp_every_days: int | None
    cover_days_per_month: float
    cover_reassign_share: float
    staff_patient_share: float
    hard_negatives: HardNegatives = field(default_factory=HardNegatives)

    def sessions_on(self, weekday: int) -> tuple[Session, ...]:
        """Monday is 0."""
        if weekday == 6:
            return self.sunday
        if weekday == 5:
            return self.saturday
        return self.weekday


@dataclass(frozen=True)
class ClinicSpec:
    index: int
    profile: Profile


@dataclass(frozen=True)
class AttackConfig:
    """Insider attack campaigns (SPEC 7). Types are numbered 1 to 10 as in the SPEC."""

    campaigns_per_clinic_month: float = 0.0
    first_day: int = 14
    types: tuple[int, ...] = tuple(range(1, 11))
    # None draws mimicry uniformly from [0, 1]; a tuple draws from those values.
    mimicry: tuple[float, ...] | None = None
    # Share of snooping accesses (types 1, 2 and 6) moved to a day when the patient has an
    # unrelated appointment, where front-desk or queue evidence may explain them by chance.
    incidental_share: float = 0.0


@dataclass(frozen=True)
class RealismConfig:
    """Generator settings that decide how hard the explanation layer's job is (author chosen)."""

    # Multiplies every benign hard-negative generator: covering days, after-hours break-glass,
    # nurse lab follow-ups, pharmacy checks, month-end billing and staff self-views.
    hard_negative_scale: float = 1.0
    # When set, these replace the value in every clinic profile.
    no_show_rate: float | None = None
    cancel_rate: float | None = None
    cover_days_per_month: float | None = None

    def apply(self, profile: Profile) -> Profile:
        s = self.hard_negative_scale
        hn = profile.hard_negatives
        return replace(
            profile,
            no_show_rate=profile.no_show_rate if self.no_show_rate is None else self.no_show_rate,
            cancel_rate=profile.cancel_rate if self.cancel_rate is None else self.cancel_rate,
            cover_days_per_month=(
                profile.cover_days_per_month
                if self.cover_days_per_month is None
                else self.cover_days_per_month
            )
            * s,
            hard_negatives=HardNegatives(
                after_hours_emergencies_per_month=hn.after_hours_emergencies_per_month * s,
                nurse_lab_followup_rate=min(1.0, hn.nurse_lab_followup_rate * s),
                pharmacy_checks_per_day=hn.pharmacy_checks_per_day * s,
                month_end_billing_share=min(1.0, hn.month_end_billing_share * s),
                staff_self_view_per_month=hn.staff_self_view_per_month * s,
            ),
        )


@dataclass(frozen=True)
class RunConfig:
    name: str
    seed: int
    start_date: date
    days: int
    timezone: str
    clinics: tuple[ClinicSpec, ...]
    attacks: AttackConfig = field(default_factory=AttackConfig)
    realism: RealismConfig = field(default_factory=RealismConfig)


def _sessions(items: list[list[str]]) -> tuple[Session, ...]:
    return tuple(Session(time.fromisoformat(a), time.fromisoformat(b)) for a, b in items)


def parse_profile(data: dict[str, Any]) -> Profile:
    staff = {role: int(data["staff"].get(role, 0)) for role in ROLES}
    if staff["doctor"] < 1 or staff["reception"] < 1:
        raise ValueError("a clinic needs at least one doctor and one receptionist")
    sessions = data["sessions"]
    camp = data.get("health_camp_every_days")
    return Profile(
        name=str(data["name"]),
        staff=staff,
        weekday=_sessions(sessions["weekday"]),
        saturday=_sessions(sessions.get("saturday", sessions["weekday"])),
        sunday=_sessions(sessions.get("sunday", [])),
        slot_minutes=int(data["slot_minutes"]),
        visits_per_doctor_session=float(data["visits_per_doctor_session"]),
        walkin_share=float(data["walkin_share"]),
        no_show_rate=float(data["no_show_rate"]),
        cancel_rate=float(data["cancel_rate"]),
        initial_patients=int(data["initial_patients"]),
        new_patient_share=float(data["new_patient_share"]),
        chronic_share=float(data["chronic_share"]),
        anc_share=float(data["anc_share"]),
        portal_share=float(data["portal_share"]),
        lab_order_rate=float(data["lab_order_rate"]),
        referral_rate=float(data.get("referral_rate", 0.0)),
        acute_followup_rate=float(data["acute_followup_rate"]),
        health_camp_every_days=int(camp) if camp else None,
        cover_days_per_month=float(data.get("cover_days_per_month", 0.0)),
        cover_reassign_share=float(data.get("cover_reassign_share", 0.6)),
        staff_patient_share=float(data.get("staff_patient_share", 0.0)),
        hard_negatives=HardNegatives(**data.get("hard_negatives", {})),
    )


def load_profile(path: Path) -> Profile:
    return parse_profile(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_config(path: Path, profiles_dir: Path | None = None) -> RunConfig:
    """Profiles are looked up by name in `profiles_dir` (default: the profiles folder next to the
    config folder)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles_dir = profiles_dir or path.resolve().parent.parent / "profiles"
    clinics: list[ClinicSpec] = []
    for group in data["clinics"]:
        profile = load_profile(profiles_dir / f"{group['profile']}.yaml")
        for _ in range(int(group["count"])):
            clinics.append(ClinicSpec(len(clinics), profile))
    return RunConfig(
        name=str(data["name"]),
        seed=int(data["seed"]),
        start_date=date.fromisoformat(str(data["start_date"])),
        days=int(data["days"]),
        timezone=str(data.get("timezone", "Asia/Kolkata")),
        clinics=tuple(clinics),
        attacks=parse_attacks(data.get("attacks") or {}),
        realism=parse_realism(data.get("realism") or {}),
    )


def parse_realism(data: dict[str, Any]) -> RealismConfig:
    def optional(key: str) -> float | None:
        value = data.get(key)
        return None if value is None else float(value)

    scale = float(data.get("hard_negative_scale", 1.0))
    if scale < 0:
        raise ValueError("hard_negative_scale must not be negative")
    return RealismConfig(
        hard_negative_scale=scale,
        no_show_rate=optional("no_show_rate"),
        cancel_rate=optional("cancel_rate"),
        cover_days_per_month=optional("cover_days_per_month"),
    )


def parse_attacks(data: dict[str, Any]) -> AttackConfig:
    types = tuple(int(t) for t in data.get("types", range(1, 11)))
    if not types or not set(types) <= set(range(1, 11)):
        raise ValueError("attack types must be between 1 and 10")
    mimicry = data.get("mimicry", "uniform")
    levels = None if mimicry == "uniform" else tuple(float(m) for m in mimicry)
    if levels is not None and not all(0.0 <= m <= 1.0 for m in levels):
        raise ValueError("mimicry levels must be in [0, 1]")
    return AttackConfig(
        campaigns_per_clinic_month=float(data.get("campaigns_per_clinic_month", 0.0)),
        first_day=int(data.get("first_day", 14)),
        types=types,
        mimicry=levels,
        incidental_share=float(data.get("incidental_share", 0.0)),
    )

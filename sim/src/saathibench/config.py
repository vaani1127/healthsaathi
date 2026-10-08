"""Run configuration and clinic profiles (YAML). See sim/configs and sim/profiles."""

from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml

ROLES = ("doctor", "nurse", "reception", "lab_tech", "clinic_admin")


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


@dataclass(frozen=True)
class RunConfig:
    name: str
    seed: int
    start_date: date
    days: int
    timezone: str
    clinics: tuple[ClinicSpec, ...]
    attacks: AttackConfig = field(default_factory=AttackConfig)


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
    )

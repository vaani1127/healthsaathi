"""SimPy model of one clinic over the simulated period (SPEC 7, benign behaviour).

Simulation time is in minutes from local midnight of the first day. Every workflow step writes the
same rows the product would write, and every look at a patient record writes an access event
through `access()`, which applies the product's policy. Benign situations that are expected to
look unusual (the hard negatives) also write a row to labels/benign_scenarios.
"""

import json
import math
import random
from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import simpy
import yaml

from saathibench.attacks import AttackTag, plan_campaigns
from saathibench.config import ClinicSpec, RunConfig, Session
from saathibench.ids import Ids
from saathibench.policy import Policy
from saathibench.recorder import Recorder, Row

NAMES: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parent / "data" / "names.yaml").read_text(encoding="utf-8")
)

SERVICES = (
    ("Consultation", 30000),
    ("Follow-up consultation", 20000),
    ("Dressing", 15000),
    ("ECG", 25000),
    ("Injection", 10000),
)
ACUTE = ("fever", "upper respiratory infection", "gastritis", "skin rash", "back pain", "injury")
CHRONIC = (("I10", "hypertension"), ("E11", "type 2 diabetes"), ("J45", "asthma"))
DRUGS = (
    "Paracetamol 500 mg",
    "Amlodipine 5 mg",
    "Metformin 500 mg",
    "Cetirizine 10 mg",
    "Pantoprazole 40 mg",
    "Iron and folic acid",
    "Salbutamol inhaler",
    "Amoxicillin 500 mg",
)
TESTS = ("CBC", "Blood sugar fasting", "HbA1c", "Lipid profile", "Urine routine", "Hb")
# Share of staff with one to three relatives registered as patients of their own clinic.
RELATIVE_SHARE = 0.5
WARDS = 40
# Per staff member, how many minute-of-day samples are kept to describe their usual hours.
HOUR_SAMPLES = 400
AGE_BANDS = ((0, 14, 0.24), (15, 29, 0.27), (30, 44, 0.21), (45, 59, 0.16), (60, 85, 0.12))
ACTIVE_ROLES = ("doctor", "nurse", "reception", "lab_tech")


@dataclass
class Staff:
    user_id: str
    role: str
    name: str
    desk_device: str
    phone_device: str | None = None
    surname: str = ""
    home: str = ""
    ward: int = 0


@dataclass
class Patient:
    id: str
    sex: str
    age: int
    kind: str  # chronic, anc or acute
    user_id: str | None = None
    staff_user: bool = False
    primary_doctor: str | None = None
    next_due: date | None = None
    anc_left: int = 0
    visits: int = 0
    has_results: bool = False
    last_rx_at: datetime | None = None
    care_team: set[str] = field(default_factory=set)
    busy_days: set[date] = field(default_factory=set)
    surname: str = ""
    ward: int = 0


@dataclass
class Seen:
    """A visit once the patient is at the desk: both are known."""

    appointment: Row
    patient: Patient
    doctor: Staff
    cover: bool


@dataclass
class Visit:
    """A booked visit has its appointment and patient; a walk-in gets them at the desk."""

    appointment: Row | None
    patient: Patient | None
    doctor: Staff
    arrival: float
    walk_in: bool
    new_patient: bool
    cover: bool = False


class ClinicSim:
    def __init__(self, spec: ClinicSpec, cfg: RunConfig, out: Path, policy: Policy) -> None:
        self.spec = spec
        self.p = spec.profile
        self.cfg = cfg
        self.policy = policy
        self.rng = random.Random(f"{cfg.seed}:{spec.index}")
        self.ids = Ids(self.rng)
        self.key = f"c{spec.index:03d}"
        self.rec = Recorder(out, self.key)
        self.tz = ZoneInfo(cfg.timezone)
        self.epoch = datetime.combine(cfg.start_date, time.min, tzinfo=self.tz)
        self.clinic_id = self.ids.new(self.epoch - timedelta(days=800))
        self.ip_hash = self.ids.hex()
        self.staff: dict[str, list[Staff]] = defaultdict(list)
        self.patients: list[Patient] = []
        self.by_user: dict[str, Patient] = {}
        self.sessions: dict[tuple[str, date, str], str] = {}
        self.home_ip: dict[str, str] = {}
        self.future: dict[tuple[date, str], list[tuple[Row, Patient]]] = defaultdict(list)
        self.tokens: dict[date, int] = defaultdict(int)
        self.invoices_by_month: dict[tuple[int, int], list[tuple[Row, Patient]]] = defaultdict(list)
        self.prescribed: list[Patient] = []
        self.notice_id = self.ids.new(self.epoch - timedelta(days=700))
        self.services: list[tuple[str, int]] = []
        self.patient_devices: dict[str, str] = {}
        self.relatives: dict[str, list[Patient]] = defaultdict(list)
        # History the attack planner uses: who treated whom, signed notes, usual hours and volume.
        self.treated: dict[str, list[tuple[Patient, datetime]]] = defaultdict(list)
        self.notes: dict[str, list[tuple[str, str, str, datetime]]] = defaultdict(list)
        self.hours: dict[str, list[float]] = defaultdict(list)
        self.daily: dict[tuple[str, date], int] = defaultdict(int)
        self.staff_patients: list[Patient] = []
        self.env = simpy.Environment()

    # Time helpers ---------------------------------------------------------------------------

    def utc(self, minutes: float) -> datetime:
        return (self.epoch + timedelta(minutes=minutes)).astimezone(UTC)

    def minutes(self, day: date, at: time) -> float:
        return (day - self.cfg.start_date).days * 1440 + at.hour * 60 + at.minute

    def local_day(self, minutes: float) -> date:
        return self.cfg.start_date + timedelta(days=int(minutes // 1440))

    def poisson(self, mean: float) -> int:
        # Knuth for small means, normal approximation for large ones.
        if mean <= 0:
            return 0
        if mean > 30:
            return max(0, round(self.rng.gauss(mean, math.sqrt(mean))))
        limit, k, prod = math.exp(-mean), 0, self.rng.random()
        while prod > limit:
            k += 1
            prod *= self.rng.random()
        return k

    # Access events --------------------------------------------------------------------------

    def _session(self, user_id: str, device_id: str, at: datetime) -> str:
        key = (user_id, at.astimezone(self.tz).date(), device_id)
        found = self.sessions.get(key)
        if found is not None:
            return found
        started = at - timedelta(minutes=self.rng.uniform(1, 20))
        session_id = self.ids.new(started)
        self.rec.add(
            "sessions",
            {
                "id": session_id,
                "user_id": user_id,
                "device_id": device_id,
                "family_id": session_id,
                "expires_at": started + timedelta(days=14),
                "revoked_at": None,
                "created_at": started,
            },
        )
        self.sessions[key] = session_id
        return session_id

    def access(
        self,
        user_id: str,
        role: str,
        patient: Patient,
        resource: str,
        action: str,
        at: datetime,
        *,
        device: str,
        ip_hash: str | None = None,
        break_glass_id: str | None = None,
        scenario: str | None = None,
        attack: AttackTag | None = None,
    ) -> str:
        allowed = self.policy.allows(role, resource, action, break_glass_id is not None)
        event_id = self.ids.new(at)
        if role != "patient" and attack is None:
            local = at.astimezone(self.tz)
            samples = self.hours[user_id]
            minute = local.hour * 60 + local.minute
            if len(samples) < HOUR_SAMPLES:
                samples.append(minute)
            else:
                samples[self.rng.randrange(HOUR_SAMPLES)] = minute
            self.daily[(user_id, local.date())] += 1
        self.rec.add(
            "access_events",
            {
                "id": event_id,
                "clinic_id": self.clinic_id,
                "user_id": user_id,
                "role": role,
                "patient_id": patient.id,
                "resource_type": resource,
                "action": action,
                "at": at,
                "session_id": self._session(user_id, device, at),
                "device_id": device,
                "ip_hash": ip_hash or self.ip_hash,
                "request_id": self.ids.hex(),
                "policy_version": self.policy.sha256,
                "decision": "allow" if allowed else "deny",
                "break_glass_id": break_glass_id,
            },
        )
        if scenario is not None:
            self.rec.add("benign_scenarios", {"access_event_id": event_id, "scenario": scenario})
        self.rec.add(
            "access_labels",
            {
                "access_event_id": event_id,
                "is_attack": attack is not None,
                "attack_type": attack.attack_type if attack else None,
                "campaign_id": attack.campaign_id if attack else None,
                "mimicry": attack.mimicry if attack else None,
            },
        )
        return event_id

    def staff_access(
        self,
        who: Staff,
        patient: Patient,
        items: list[tuple[str, str]],
        at: datetime,
        *,
        scenario: str | None = None,
        phone: bool = False,
        ip_hash: str | None = None,
        break_glass_id: str | None = None,
        attack: AttackTag | None = None,
        device: str | None = None,
        gap: tuple[float, float] = (4, 40),
    ) -> datetime:
        """A run of accesses a few seconds apart. Returns the time after the last one."""
        device = device or (who.phone_device if phone and who.phone_device else who.desk_device)
        if who.user_id == patient.user_id and scenario is None:
            scenario = "staff_self_view"
        for resource, action in items:
            at += timedelta(seconds=self.rng.uniform(*gap))
            self.access(
                who.user_id,
                who.role,
                patient,
                resource,
                action,
                at,
                device=device,
                ip_hash=ip_hash,
                break_glass_id=break_glass_id,
                scenario=scenario,
                attack=attack,
            )
        return at

    # Setup ----------------------------------------------------------------------------------

    def _name(self, sex: str) -> str:
        given = self.rng.choice(NAMES["female" if sex == "F" else "male"])
        return f"{given} {self.rng.choice(NAMES['surnames'])}"

    def _phone(self) -> str:
        return "+91 9" + "".join(str(self.rng.randrange(10)) for _ in range(9))

    def _user(self, name: str, email: str, created: datetime) -> str:
        user_id = self.ids.new(created)
        self.rec.add(
            "users",
            {
                "id": user_id,
                "email": email,
                "phone": self._phone(),
                "name": name,
                "is_active": True,
                "created_at": created,
            },
        )
        return user_id

    def _device(self, user_id: str, kind: str, created: datetime) -> str:
        device_id = self.ids.new(created)
        agent = (
            "Mozilla/5.0 (Linux; Android 13) Chrome/128 Mobile"
            if kind == "phone"
            else "Mozilla/5.0 (Windows NT 10.0) Chrome/128"
        )
        self.rec.add(
            "devices",
            {
                "id": device_id,
                "user_id": user_id,
                "fingerprint_hash": self.ids.hex(),
                "user_agent": agent,
                "first_seen": created,
                "last_seen": self.utc(self.cfg.days * 1440),
            },
        )
        return device_id

    def setup(self) -> None:
        founded = self.epoch - timedelta(days=self.rng.randint(400, 760))
        city, state = self.rng.choice(NAMES["cities"])
        self.rec.add(
            "clinics",
            {
                "id": self.clinic_id,
                "name": f"SaathiBench {self.p.name.replace('_', ' ')} {self.spec.index:03d}",
                "city": city,
                "state": state,
                "timezone": self.cfg.timezone,
                "created_at": founded,
            },
        )
        for role, count in self.p.staff.items():
            for i in range(count):
                sex = self.rng.choice("FM")
                surname = self.rng.choice(NAMES["surnames"])
                given = self.rng.choice(NAMES["female" if sex == "F" else "male"])
                name = f"{given} {surname}"
                ward = self.rng.randint(1, WARDS)
                home = f"{self.rng.randint(1, 400)}, Ward {ward}"
                joined = founded + timedelta(days=self.rng.randint(0, 300))
                email = f"{role}{i + 1}.{self.key}@saathibench.test"
                user_id = self._user(("Dr. " if role == "doctor" else "") + name, email, joined)
                self.rec.add(
                    "memberships",
                    {
                        "id": self.ids.new(joined),
                        "clinic_id": self.clinic_id,
                        "user_id": user_id,
                        "role": role,
                        "is_active": True,
                        "created_at": joined,
                    },
                )
                phone = self._device(user_id, "phone", joined) if role == "doctor" else None
                member = Staff(
                    user_id,
                    role,
                    name,
                    self._device(user_id, "desk", joined),
                    phone,
                    surname=surname,
                    home=home,
                    ward=ward,
                )
                self.staff[role].append(member)
                if role == "doctor":
                    self.rec.add(
                        "staff_profiles",
                        {
                            "id": self.ids.new(joined),
                            "clinic_id": self.clinic_id,
                            "user_id": user_id,
                            "registration_no": f"SB-{self.rng.randrange(10**5, 10**6)}",
                            "specialization": self.rng.choice(
                                ("General medicine", "Paediatrics", "Gynaecology", "Dermatology")
                            ),
                            "department": "OPD",
                        },
                    )
        admin = self._admin()
        for name, price in SERVICES:
            service_id = self.ids.new(founded)
            self.services.append((service_id, price))
            self.rec.add(
                "services",
                {
                    "id": service_id,
                    "clinic_id": self.clinic_id,
                    "name": name,
                    "price_paise": price,
                    "is_active": True,
                },
            )
        for doctor in self.staff["doctor"]:
            for weekday in range(7):
                for session in self.p.sessions_on(weekday):
                    self.rec.add(
                        "schedules",
                        {
                            "id": self.ids.new(founded),
                            "clinic_id": self.clinic_id,
                            "doctor_user_id": doctor.user_id,
                            "weekday": weekday,
                            "start_time": session.start,
                            "end_time": session.end,
                            "slot_minutes": self.p.slot_minutes,
                            "status": "active",
                            "created_by": admin.user_id,
                            "created_at": founded,
                            "updated_at": founded,
                        },
                    )
        for _ in range(self.p.initial_patients):
            registered = self.epoch - timedelta(days=self.rng.uniform(1, 720))
            self.new_patient(registered, self.rng.choice(self.staff["reception"]))
        self._staff_as_patients()

    def _admin(self) -> Staff:
        return self.rng.choice(self.staff["clinic_admin"] or self.staff["reception"])

    def _staff_as_patients(self) -> None:
        """Some staff are patients of their own clinic, and some have relatives who are (same
        surname and address). Both are ordinary patients in the workflow."""
        clerk = self.staff["reception"][0]
        for role in ACTIVE_ROLES:
            for member in self.staff[role]:
                if self.rng.random() < self.p.staff_patient_share:
                    registered = self.epoch - timedelta(days=self.rng.uniform(30, 300))
                    patient = self.new_patient(
                        registered,
                        clerk,
                        user_id=member.user_id,
                        surname=member.surname,
                        address=member.home,
                    )
                    patient.staff_user = True
                    self.staff_patients.append(patient)
                if self.rng.random() < RELATIVE_SHARE:
                    for _ in range(self.rng.randint(1, 3)):
                        registered = self.epoch - timedelta(days=self.rng.uniform(5, 700))
                        relative = self.new_patient(
                            registered, clerk, surname=member.surname, address=member.home
                        )
                        self.relatives[member.user_id].append(relative)

    def new_patient(
        self,
        at: datetime,
        by: Staff,
        *,
        walk_in_day: date | None = None,
        user_id: str | None = None,
        surname: str | None = None,
        address: str | None = None,
    ) -> Patient:
        sex = self.rng.choice("FM")
        surname = surname or self.rng.choice(NAMES["surnames"])
        if address is None:
            address = f"{self.rng.randint(1, 400)}, Ward {self.rng.randint(1, WARDS)}"
        low, high, _ = self.rng.choices(AGE_BANDS, weights=[b[2] for b in AGE_BANDS])[0]
        age = self.rng.randint(low, high)
        roll = self.rng.random()
        if sex == "F" and 18 <= age <= 40 and roll < self.p.anc_share * 4:
            kind = "anc"
        elif age >= 30 and roll < self.p.anc_share * 4 + self.p.chronic_share * 1.6:
            kind = "chronic"
        else:
            kind = "acute"
        if user_id is None and self.rng.random() < self.p.portal_share:
            user_id = self._user(self._name(sex), f"patient.{self.ids.hex(6)}@saathibench.test", at)
        patient = Patient(
            self.ids.new(at),
            sex,
            age,
            kind,
            user_id=user_id,
            surname=surname,
            ward=int(address.rsplit(" ", 1)[1]),
        )
        if user_id is not None:
            self.by_user[user_id] = patient
            self.home_ip[patient.id] = self.ids.hex()
        dob = at.astimezone(self.tz).date() - timedelta(days=age * 365 + self.rng.randint(0, 364))
        self.rec.add(
            "patients",
            {
                "id": patient.id,
                "clinic_id": self.clinic_id,
                "user_id": user_id,
                "mrn": f"{self.key.upper()}-{len(self.patients) + 1:06d}",
                "name": f"{self.rng.choice(NAMES['female' if sex == 'F' else 'male'])} {surname}",
                "dob": dob,
                "sex": "female" if sex == "F" else "male",
                "phone": self._phone(),
                "address": address,
                "abha_number": None,
                "emergency_contact": self._name(self.rng.choice("FM")),
                "created_by": by.user_id,
                "created_at": at,
            },
        )
        self.rec.add(
            "consents",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "patient_id": patient.id,
                "notice_id": self.notice_id,
                "granted_at": at,
                "channel": "reception",
                "withdrawn_at": None,
            },
        )
        if kind == "chronic":
            code, text = self.rng.choice(CHRONIC)
            self.rec.add(
                "conditions",
                {
                    "id": self.ids.new(at),
                    "clinic_id": self.clinic_id,
                    "patient_id": patient.id,
                    "code": code,
                    "text": text,
                    "onset": dob + timedelta(days=365 * max(age - 3, 1)),
                    "status": "active",
                    "recorded_by": by.user_id,
                    "created_at": at,
                },
            )
        if at < self.epoch.astimezone(UTC) and kind in ("chronic", "anc"):
            # Existing chronic and antenatal patients already have a doctor and a due date.
            doctor = self.rng.choice(self.staff["doctor"])
            patient.primary_doctor = doctor.user_id
            gap = 28 if kind == "anc" else 30
            patient.next_due = self.cfg.start_date + timedelta(days=self.rng.randint(0, gap))
            patient.anc_left = self.rng.randint(2, 7) if kind == "anc" else 0
            self._care_team(patient, doctor, at + timedelta(hours=1))
        if self.rng.random() < 0.08:
            self.rec.add(
                "allergies",
                {
                    "id": self.ids.new(at),
                    "clinic_id": self.clinic_id,
                    "patient_id": patient.id,
                    "substance": self.rng.choice(("Penicillin", "Sulfa drugs", "Peanuts", "Dust")),
                    "reaction": self.rng.choice(("Rash", "Swelling", "Breathlessness")),
                    "severity": self.rng.choice(("mild", "moderate", "severe")),
                    "is_active": True,
                    "recorded_by": by.user_id,
                    "created_at": at,
                },
            )
        self.patients.append(patient)
        if walk_in_day is not None:
            patient.busy_days.add(walk_in_day)
        return patient

    def _care_team(self, patient: Patient, doctor: Staff, at: datetime) -> None:
        if doctor.user_id in patient.care_team:
            return
        patient.care_team.add(doctor.user_id)
        self.rec.add(
            "care_team_assignments",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "patient_id": patient.id,
                "user_id": doctor.user_id,
                "role": "doctor",
                "starts_at": at,
                "ends_at": None,
                "status": "active",
                "created_by": doctor.user_id,
                "created_at": at,
                "updated_at": at,
            },
        )

    # Days -----------------------------------------------------------------------------------

    def run(self) -> dict[str, int]:
        self.setup()
        self.reception_desk = simpy.Resource(self.env, capacity=len(self.staff["reception"]))
        self.nurse_station = simpy.Resource(self.env, capacity=max(1, len(self.staff["nurse"])))
        self.lab = simpy.Resource(self.env, capacity=max(1, len(self.staff["lab_tech"])))
        self.rooms = {d.user_id: simpy.Resource(self.env, capacity=1) for d in self.staff["doctor"]}
        self.env.process(self.calendar())
        plan_campaigns(self)
        self.env.run(until=self.cfg.days * 1440)
        return self.rec.close()

    def calendar(self) -> Generator[simpy.Event, Any, None]:
        for offset in range(self.cfg.days):
            day = self.cfg.start_date + timedelta(days=offset)
            self.plan_day(day, offset)
            yield self.env.timeout(1440)

    def _is_cover_day(self) -> bool:
        doctors = len(self.staff["doctor"])
        return doctors >= 2 and self.rng.random() < self.p.cover_days_per_month / 26

    def plan_day(self, day: date, offset: int) -> None:
        sessions = self.p.sessions_on(day.weekday())
        doctors = list(self.staff["doctor"])
        absent: Staff | None = None
        cover: Staff | None = None
        if sessions and self._is_cover_day():
            absent = self.rng.choice(doctors)
            cover = self.rng.choice([d for d in doctors if d is not absent])
        camp = bool(
            self.p.health_camp_every_days
            and offset % self.p.health_camp_every_days == self.p.health_camp_every_days // 2
            and sessions
        )
        for index, session in enumerate(sessions):
            self._shifts(day, session, absent)
            for doctor in doctors:
                self._plan_session(day, session, doctor, absent, cover, camp)
            if camp and index == 0:
                self._camp(day, session)
        self._hard_negatives(day, sessions)

    def _shifts(self, day: date, session: Session, absent: Staff | None) -> None:
        start = self.minutes(day, session.start) - 15
        end = self.minutes(day, session.end) + 30
        admin = self._admin()
        for role in ACTIVE_ROLES:
            for member in self.staff[role]:
                if member is absent:
                    continue
                self.rec.add(
                    "shifts",
                    {
                        "id": self.ids.new(self.utc(start - 1440)),
                        "clinic_id": self.clinic_id,
                        "user_id": member.user_id,
                        "starts_at": self.utc(start),
                        "ends_at": self.utc(end),
                        "role": role,
                        "status": "scheduled",
                        "created_by": admin.user_id,
                        "created_at": self.utc(start - 1440),
                        "updated_at": self.utc(start - 1440),
                    },
                )

    def _pick_booked(self, day: date, doctor: Staff) -> Patient:
        for _ in range(8):
            if self.rng.random() < 0.35:
                due = [
                    p
                    for p in self._sample(40)
                    if p.next_due is not None
                    and p.next_due <= day + timedelta(days=3)
                    and p.primary_doctor in (None, doctor.user_id)
                    and day not in p.busy_days
                ]
                if due:
                    return due[0]
            patient = self.rng.choice(self.patients)
            if day not in patient.busy_days:
                return patient
        return self.rng.choice(self.patients)

    def _sample(self, n: int) -> list[Patient]:
        return [self.rng.choice(self.patients) for _ in range(n)]

    def _plan_session(
        self,
        day: date,
        session: Session,
        doctor: Staff,
        absent: Staff | None,
        cover: Staff | None,
        camp: bool,
    ) -> None:
        start = self.minutes(day, session.start)
        end = self.minutes(day, session.end)
        mean = self.p.visits_per_doctor_session
        seen_by = cover if doctor is absent and cover is not None else doctor
        reception = self.staff["reception"]

        followups = [
            (a, p)
            for a, p in self.future.pop((day, doctor.user_id), [])
            if start <= self._m(a) < end
        ]
        leftover = [
            (a, p)
            for a, p in self.future.get((day, doctor.user_id), [])
            if not start <= self._m(a) < end
        ]
        if leftover:
            self.future[(day, doctor.user_id)] = leftover
        booked = max(0, self.poisson(mean * (1 - self.p.walkin_share)) - len(followups))
        slots = [
            start + i * self.p.slot_minutes
            for i in range(max(1, int((end - start) // self.p.slot_minutes)))
        ]
        visits: list[Visit] = []
        taken = {self._m(a) for a, _ in followups}
        free = [s for s in slots if s not in taken]
        self.rng.shuffle(free)
        for i in range(booked):
            slot = free[i] if i < len(free) else end - self.p.slot_minutes + i
            patient = self._pick_booked(day, doctor)
            booked_at = self._booking_time(day, slot)
            by_patient = (
                patient.user_id is not None and not patient.staff_user and self.rng.random() < 0.3
            )
            clerk = self.rng.choice(reception)
            if self.rng.random() < self.p.new_patient_share * 0.4:
                patient = self.new_patient(self.utc(booked_at), clerk)
                by_patient = False
            appt = self._appointment(
                patient,
                doctor,
                slot,
                "followup" if patient.kind != "acute" and patient.visits else "new",
                "patient" if by_patient else "reception",
                patient.user_id if by_patient and patient.user_id else clerk.user_id,
                booked_at,
            )
            patient.busy_days.add(day)
            if by_patient:
                self._portal_view(patient, self.utc(booked_at), [("demographics", "view")])
            else:
                self.staff_access(clerk, patient, [("demographics", "view")], self.utc(booked_at))
            followups.append((appt, patient))
        for appt, patient in followups:
            if self.rng.random() < self.p.cancel_rate:
                cancelled = self._m(appt) - self.rng.uniform(60, 2000)
                cancelled = max(cancelled, self._m_created(appt) + 5)
                appt["status"] = "cancelled"
                appt["updated_at"] = self.utc(cancelled)
                clerk = self.rng.choice(reception)
                self.staff_access(clerk, patient, [("demographics", "edit")], self.utc(cancelled))
                continue
            is_cover = doctor is absent
            if is_cover and cover is not None and self.rng.random() < self.p.cover_reassign_share:
                moved = start - self.rng.uniform(20, 90)
                appt["doctor_user_id"] = cover.user_id
                appt["updated_at"] = self.utc(moved)
            arrival = self._m(appt) + self.rng.uniform(-15, 20)
            visits.append(Visit(appt, patient, seen_by, arrival, False, False, is_cover))
        walk_ins = self.poisson(mean * self.p.walkin_share * (2.5 if camp else 1.0))
        for _ in range(walk_ins):
            arrival = self.rng.uniform(start, max(start + 1, end - 30))
            new = self.rng.random() < self.p.new_patient_share * (2.0 if camp else 1.0)
            walk_in: Patient | None = None
            if not new:
                walk_in = self.rng.choice(self.patients)
                if day in walk_in.busy_days:
                    continue
                walk_in.busy_days.add(day)
            visits.append(Visit(None, walk_in, seen_by, arrival, True, new, doctor is absent))
        for visit in visits:
            self.env.process(self.visit(visit, day))
        self.env.process(self.chart_prep(seen_by, start, [v for v in visits if not v.walk_in]))

    def _since_epoch(self, at: datetime) -> float:
        return (at - self.epoch).total_seconds() / 60

    def _m(self, appt: Row) -> float:
        return self._since_epoch(appt["slot_start"])

    def _m_created(self, appt: Row) -> float:
        return self._since_epoch(appt["created_at"])

    def _booking_time(self, day: date, slot: float) -> float:
        days_before = self.rng.choice((0, 1, 1, 2, 3, 4, 6))
        booked = self.minutes(day - timedelta(days=days_before), time(9)) + self.rng.uniform(0, 600)
        return min(booked, slot - 45)

    def _appointment(
        self,
        patient: Patient,
        doctor: Staff,
        slot: float,
        kind: str,
        source: str,
        created_by: str,
        created: float,
    ) -> Row:
        at = self.utc(created)
        return self.rec.add(
            "appointments",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "patient_id": patient.id,
                "doctor_user_id": doctor.user_id,
                "slot_start": self.utc(slot),
                "slot_end": self.utc(slot + self.p.slot_minutes),
                "kind": kind,
                "status": "booked",
                "source": source,
                "created_by": created_by,
                "created_at": at,
                "updated_at": at,
            },
        )

    # Visit ----------------------------------------------------------------------------------

    def visit(self, v: Visit, day: date) -> Generator[simpy.Event, Any, None]:
        yield self.env.timeout(max(0.0, v.arrival - self.env.now))
        if v.appointment is not None and self.rng.random() < self.p.no_show_rate:
            v.appointment["status"] = "no_show"
            v.appointment["updated_at"] = self.utc(self._m(v.appointment) + 120)
            return
        with self.reception_desk.request() as turn:
            yield turn
            clerk = self.rng.choice(self.staff["reception"])
            t = self.utc(self.env.now)
            if v.patient is None:
                patient = self.new_patient(t, clerk, walk_in_day=day)
                t = self.staff_access(
                    clerk, patient, [("demographics", "create"), ("consent", "create")], t
                )
            else:
                patient = v.patient
                t = self.staff_access(clerk, patient, [("demographics", "view")], t)
            appointment = v.appointment
            if appointment is None:
                created = (t - self.epoch.astimezone(UTC)).total_seconds() / 60 + 0.5
                appointment = self._appointment(
                    patient, v.doctor, created, "walkin", "reception", clerk.user_id, created
                )
            self.tokens[day] += 1
            token = self.rec.add(
                "queue_tokens",
                {
                    "id": self.ids.new(t),
                    "clinic_id": self.clinic_id,
                    "appointment_id": appointment["id"],
                    "token_no": self.tokens[day],
                    "day": day,
                    "status": "waiting",
                    "assigned_nurse_user_id": None,
                    "created_by": clerk.user_id,
                    "created_at": t + timedelta(seconds=30),
                    "updated_at": t + timedelta(seconds=30),
                },
            )
            appointment["status"] = "checked_in"
            appointment["updated_at"] = t
            yield self.env.timeout(self.rng.uniform(1, 3))
        seen = Seen(appointment, patient, v.doctor, v.cover)

        if self.staff["nurse"]:
            with self.nurse_station.request() as turn:
                yield turn
                nurse = self.rng.choice(self.staff["nurse"])
                t = self.utc(self.env.now)
                token["assigned_nurse_user_id"] = nurse.user_id
                token["status"] = "with_nurse"
                token["updated_at"] = t
                items = [("demographics", "view"), ("allergies", "view"), ("vitals", "create")]
                if self.rng.random() < 0.02:
                    items.append(("allergies", "create"))
                t = self.staff_access(nurse, patient, items, t + timedelta(seconds=40))
                self._vitals(patient, nurse, t)
                self.treated[nurse.user_id].append((patient, t))
                yield self.env.timeout(self.rng.uniform(3, 7))

        with self.rooms[v.doctor.user_id].request() as turn:
            yield turn
            yield from self.consult(seen, token)

        with self.reception_desk.request() as turn:
            yield turn
            yield from self.billing(seen)

    def _vitals(self, patient: Patient, nurse: Staff, at: datetime) -> None:
        self.rec.add(
            "vitals",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "patient_id": patient.id,
                "encounter_id": None,
                "recorded_by": nurse.user_id,
                "recorded_at": at,
                "bp_sys": self.rng.randint(100, 165),
                "bp_dia": self.rng.randint(60, 100),
                "pulse": self.rng.randint(60, 110),
                "temp_c": round(self.rng.uniform(36.2, 39.2), 1),
                "spo2": self.rng.randint(93, 100),
                "rr": self.rng.randint(12, 24),
                "weight_kg": round(self.rng.uniform(8, 95), 1),
                "height_cm": round(self.rng.uniform(70, 185), 1),
            },
        )

    def consult(self, v: Seen, token: Row) -> Generator[simpy.Event, Any, None]:
        doctor, patient = v.doctor, v.patient
        scenario = "cover_doctor" if v.cover else None
        t = self.utc(self.env.now)
        encounter_id = self.ids.new(t)
        v.appointment["status"] = "in_consult"
        v.appointment["updated_at"] = t
        token["status"] = "with_doctor"
        token["updated_at"] = t
        reads = [("demographics", "view"), ("vitals", "view"), ("allergies", "view")]
        if patient.visits:
            reads += [("notes", "view"), ("prescriptions", "view")]
        if patient.has_results:
            reads.append(("lab", "view"))
        self.staff_access(doctor, patient, reads, t, scenario=scenario)
        yield self.env.timeout(self.rng.uniform(5, 14))
        done = self.utc(self.env.now)
        writes = [("notes", "create")]
        prescribe = self.rng.random() < 0.9
        if prescribe:
            writes.append(("prescriptions", "create"))
        order_lab = self.rng.random() < self.p.lab_order_rate
        if order_lab:
            writes.append(("lab", "create"))
        end = self.staff_access(doctor, patient, writes, done, scenario=scenario)
        common = {"clinic_id": self.clinic_id, "patient_id": patient.id}
        self.rec.add(
            "encounters",
            {
                "id": encounter_id,
                **common,
                "appointment_id": v.appointment["id"],
                "doctor_user_id": doctor.user_id,
                "started_at": t,
                "ended_at": end,
                "status": "closed",
                "created_by": doctor.user_id,
                "created_at": t,
                "updated_at": end,
            },
        )
        note_id = self.ids.new(done)
        self.notes[patient.id].append((note_id, encounter_id, doctor.user_id, end))
        self.treated[doctor.user_id].append((patient, end))
        self.rec.add(
            "clinical_notes",
            {
                "id": note_id,
                **common,
                "encounter_id": encounter_id,
                "author_user_id": doctor.user_id,
                "version": 1,
                "parent_id": None,
                "signed_at": end,
                "created_at": done,
            },
        )
        if prescribe:
            items = [{"drug": d} for d in self.rng.sample(DRUGS, self.rng.randint(1, 3))]
            self.rec.add(
                "prescriptions",
                {
                    "id": self.ids.new(done),
                    **common,
                    "encounter_id": encounter_id,
                    "author_user_id": doctor.user_id,
                    "items": json.dumps(items),
                    "signed_at": end,
                    "version": 1,
                    "parent_id": None,
                    "created_at": done,
                },
            )
            patient.last_rx_at = end
            self.prescribed.append(patient)
        if order_lab:
            order = self.rec.add(
                "lab_orders",
                {
                    "id": self.ids.new(done),
                    **common,
                    "encounter_id": encounter_id,
                    "ordered_by": doctor.user_id,
                    "tests": json.dumps(self.rng.sample(TESTS, self.rng.randint(1, 3))),
                    "status": "ordered",
                    "created_at": done,
                    "updated_at": done,
                },
            )
            self.env.process(self.lab_work(order, patient, doctor))
        others = [d for d in self.staff["doctor"] if d is not doctor]
        if others and self.rng.random() < self.p.referral_rate:
            self.env.process(self.referral(patient, doctor, self.rng.choice(others), end))
        self._book_followup(patient, doctor, end)
        if patient.kind in ("chronic", "anc"):
            patient.primary_doctor = patient.primary_doctor or doctor.user_id
            self._care_team(patient, doctor, end)
        v.appointment["status"] = "completed"
        v.appointment["updated_at"] = end
        token["status"] = "done"
        token["updated_at"] = end
        patient.visits += 1
        if patient.user_id and not patient.staff_user and self.rng.random() < 0.4:
            later = end + timedelta(hours=self.rng.uniform(1, 48))
            self._portal_view(
                patient, later, [("prescriptions", "view"), ("notes", "view"), ("billing", "view")]
            )

    def _book_followup(self, patient: Patient, doctor: Staff, at: datetime) -> None:
        if patient.kind == "anc":
            patient.anc_left -= 1
            gap = 28 if patient.anc_left > 0 else None
        elif patient.kind == "chronic":
            gap = self.rng.randint(25, 35)
        else:
            gap = (
                self.rng.randint(5, 10) if self.rng.random() < self.p.acute_followup_rate else None
            )
        if gap is None:
            patient.next_due = None
            return
        day = at.astimezone(self.tz).date() + timedelta(days=gap)
        sessions = self.p.sessions_on(day.weekday())
        if not sessions:
            day += timedelta(days=1)
            sessions = self.p.sessions_on(day.weekday())
        session = self.rng.choice(sessions)
        start, end = self.minutes(day, session.start), self.minutes(day, session.end)
        slot = start + self.p.slot_minutes * self.rng.randrange(
            max(1, int((end - start) // self.p.slot_minutes))
        )
        created = (at - self.epoch.astimezone(UTC)).total_seconds() / 60 + 0.3
        appt = self._appointment(
            patient, doctor, slot, "followup", "doctor", doctor.user_id, created
        )
        patient.next_due = day
        patient.busy_days.add(day)
        self.future[(day, doctor.user_id)].append((appt, patient))

    def billing(self, v: Seen) -> Generator[simpy.Event, Any, None]:
        clerk = self.rng.choice(self.staff["reception"])
        t = self.utc(self.env.now)
        _, price = self.services[1] if v.patient.visits > 1 else self.services[0]
        invoice = self.rec.add(
            "invoices",
            {
                "id": self.ids.new(t),
                "clinic_id": self.clinic_id,
                "patient_id": v.patient.id,
                "appointment_id": v.appointment["id"],
                "status": "issued",
                "total_paise": price,
                "created_by": clerk.user_id,
                "created_at": t,
                "updated_at": t,
            },
        )
        t = self.staff_access(clerk, v.patient, [("billing", "create")], t + timedelta(seconds=5))
        if self.rng.random() < 0.92:
            invoice["status"] = "paid"
            invoice["updated_at"] = t
            self._payment(invoice, clerk, t)
            self.staff_access(clerk, v.patient, [("billing", "print")], t)
        local = t.astimezone(self.tz)
        self.invoices_by_month[(local.year, local.month)].append((invoice, v.patient))
        yield self.env.timeout(self.rng.uniform(1, 3))

    def _payment(self, invoice: Row, clerk: Staff, at: datetime) -> None:
        self.rec.add(
            "payments",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "invoice_id": invoice["id"],
                "method": self.rng.choice(("cash", "upi", "upi", "card_offline")),
                "amount_paise": invoice["total_paise"],
                "received_by": clerk.user_id,
                "received_at": at,
            },
        )

    # Side processes -------------------------------------------------------------------------

    def chart_prep(
        self, doctor: Staff, start: float, visits: list[Visit]
    ) -> Generator[simpy.Event, Any, None]:
        """Doctors glance at the first few booked patients before the session."""
        returning = [
            (v.appointment, v.patient)
            for v in visits
            if v.appointment is not None and v.patient is not None and v.patient.visits > 0
        ][: self.rng.randint(0, 3)]
        if not returning:
            return
        yield self.env.timeout(max(0.0, start - self.rng.uniform(5, 30) - self.env.now))
        t = self.utc(self.env.now)
        for appointment, patient in returning:
            if appointment["doctor_user_id"] == doctor.user_id:
                t = self.staff_access(doctor, patient, [("notes", "view")], t)

    def _next_open(self, after: float) -> float:
        """The next moment the clinic is in session at or after `after`."""
        day = self.local_day(after)
        for _ in range(8):
            for session in self.p.sessions_on(day.weekday()):
                start, end = self.minutes(day, session.start), self.minutes(day, session.end)
                if after < end:
                    return max(after, start)
            day += timedelta(days=1)
        return after

    def lab_work(
        self, order: Row, patient: Patient, doctor: Staff
    ) -> Generator[simpy.Event, Any, None]:
        if not self.staff["lab_tech"]:
            return
        yield self.env.timeout(self.rng.uniform(5, 60))
        with self.lab.request() as turn:
            yield turn
            tech = self.rng.choice(self.staff["lab_tech"])
            t = self.utc(self.env.now)
            t = self.staff_access(tech, patient, [("demographics", "view"), ("lab", "view")], t)
            order["status"] = "collected"
            order["updated_at"] = t
        ready = self._next_open(self.env.now + self.rng.uniform(120, 1800))
        yield self.env.timeout(ready - self.env.now)
        tech = self.rng.choice(self.staff["lab_tech"])
        t = self.staff_access(tech, patient, [("lab", "create")], self.utc(self.env.now))
        result = self.rec.add(
            "lab_results",
            {
                "id": self.ids.new(t),
                "clinic_id": self.clinic_id,
                "lab_order_id": order["id"],
                "patient_id": patient.id,
                "resulted_by": tech.user_id,
                "resulted_at": t,
                "released_at": None,
            },
        )
        yield self.env.timeout(self.rng.uniform(5, 90))
        t = self.staff_access(tech, patient, [("lab", "edit")], self.utc(self.env.now))
        result["released_at"] = t
        order["status"] = "resulted"
        order["updated_at"] = t
        patient.has_results = True
        # Nurses sometimes look up yesterday's lab patients the next morning (hard negative).
        hn = self.p.hard_negatives
        if self.staff["nurse"] and self.rng.random() < hn.nurse_lab_followup_rate:
            self.env.process(self.nurse_followup(patient))
        review = self._next_open(self.env.now + self.rng.uniform(30, 720))
        yield self.env.timeout(review - self.env.now)
        self.staff_access(doctor, patient, [("lab", "view")], self.utc(self.env.now))

    def nurse_followup(self, patient: Patient) -> Generator[simpy.Event, Any, None]:
        nxt = self._next_open(self.env.now + 600)
        yield self.env.timeout(nxt + self.rng.uniform(0, 60) - self.env.now)
        nurse = self.rng.choice(self.staff["nurse"])
        items = [("demographics", "view"), ("vitals", "view")]
        if self.rng.random() < 0.1:
            items.append(("lab", "view"))  # denied by policy: nurses cannot open lab results
        self.staff_access(
            nurse, patient, items, self.utc(self.env.now), scenario="nurse_lab_followup"
        )

    def referral(
        self, patient: Patient, src: Staff, dst: Staff, at: datetime
    ) -> Generator[simpy.Event, Any, None]:
        self.rec.add(
            "referrals",
            {
                "id": self.ids.new(at),
                "clinic_id": self.clinic_id,
                "patient_id": patient.id,
                "from_user_id": src.user_id,
                "to_user_id": dst.user_id,
                "reason": "Second opinion",
                "valid_until": at + timedelta(days=14),
                "status": "active",
                "created_by": src.user_id,
                "created_at": at,
                "updated_at": at,
            },
        )
        now = (at - self.epoch.astimezone(UTC)).total_seconds() / 60
        seen = self._next_open(now + self.rng.uniform(30, 3 * 1440))
        yield self.env.timeout(max(0.0, seen - self.env.now))
        # The referral stays active until valid_until, as in the product.
        self.staff_access(
            dst,
            patient,
            [("demographics", "view"), ("notes", "view"), ("prescriptions", "view")],
            self.utc(self.env.now),
        )

    # Hard negatives -------------------------------------------------------------------------

    def _portal_view(self, patient: Patient, at: datetime, items: list[tuple[str, str]]) -> None:
        assert patient.user_id is not None
        for resource, action in items:
            at += timedelta(seconds=self.rng.uniform(5, 60))
            self.access(
                patient.user_id,
                "patient",
                patient,
                resource,
                action,
                at,
                device=self._patient_device(patient, at),
                ip_hash=self.home_ip.get(patient.id),
            )

    def _patient_device(self, patient: Patient, at: datetime) -> str:
        assert patient.user_id is not None
        device = self.patient_devices.get(patient.user_id)
        if device is None:
            device = self._device(patient.user_id, "phone", at)
            self.patient_devices[patient.user_id] = device
        return device

    def _hard_negatives(self, day: date, sessions: tuple[Session, ...]) -> None:
        hn = self.p.hard_negatives
        if self.rng.random() < hn.after_hours_emergencies_per_month / 30:
            self.env.process(self.after_hours(day))
        if sessions:
            for _ in range(self.poisson(hn.pharmacy_checks_per_day)):
                session = self.rng.choice(sessions)
                at = self.rng.uniform(
                    self.minutes(day, session.start), self.minutes(day, session.end)
                )
                self.env.process(self.pharmacy_check(day, at))
            if self._last_open_day_of_month(day):
                self.env.process(self.month_end(day, sessions[-1]))
        per_day = hn.staff_self_view_per_month / 30
        for patient in self.staff_patients:
            if self.rng.random() < per_day and sessions:
                self.env.process(self.staff_self_view(patient, day, sessions[0]))

    def _last_open_day_of_month(self, day: date) -> bool:
        nxt = day + timedelta(days=1)
        while nxt.month == day.month and not self.p.sessions_on(nxt.weekday()):
            nxt += timedelta(days=1)
        return nxt.month != day.month

    def after_hours(self, day: date) -> Generator[simpy.Event, Any, None]:
        at = self.minutes(day, time(22)) + self.rng.uniform(0, 420)
        known = [p for p in self._sample(60) if p.kind in ("chronic", "anc") and p.visits]
        if not known:
            return
        yield self.env.timeout(max(0.0, at - self.env.now))
        doctor = self.rng.choice(self.staff["doctor"])
        patient = known[0]
        t = self.utc(self.env.now)
        bg_id = self.ids.new(t)
        self.rec.add(
            "break_glass_events",
            {
                "id": bg_id,
                "clinic_id": self.clinic_id,
                "user_id": doctor.user_id,
                "patient_id": patient.id,
                "reason_code": "emergency",
                "reason_text": "Family called at night, checking medicines and allergies",
                "at": t,
            },
        )
        items = [
            ("demographics", "view"),
            ("allergies", "view"),
            ("prescriptions", "view"),
            ("vitals", "view"),
            ("notes", "view"),
        ]
        self.staff_access(
            doctor,
            patient,
            items,
            t,
            scenario="after_hours_emergency",
            phone=True,
            ip_hash=self.home_ip.setdefault(doctor.user_id, self.ids.hex()),
            break_glass_id=bg_id,
        )

    def pharmacy_check(self, day: date, at: float) -> Generator[simpy.Event, Any, None]:
        if not self.staff["nurse"]:
            return
        recent = [
            p for p in self.prescribed[-400:] if p.last_rx_at is not None and day not in p.busy_days
        ]
        if not recent:
            return
        yield self.env.timeout(max(0.0, at - self.env.now))
        patient = self.rng.choice(recent)
        nurse = self.rng.choice(self.staff["nurse"])
        self.staff_access(
            nurse,
            patient,
            [("demographics", "view"), ("allergies", "view")],
            self.utc(self.env.now),
            scenario="pharmacy_check",
        )

    def month_end(self, day: date, session: Session) -> Generator[simpy.Event, Any, None]:
        """Month-end reconciliation: each receptionist opens a share of the month's invoices,
        unpaid ones first, up to what one person gets through in an evening."""
        yield self.env.timeout(max(0.0, self.minutes(day, session.end) + 20 - self.env.now))
        invoices = self.invoices_by_month.get((day.year, day.month), [])
        share = self.p.hard_negatives.month_end_billing_share
        unpaid = [item for item in invoices if item[0]["status"] == "issued"]
        paid = [item for item in invoices if item[0]["status"] != "issued"]
        sample = [item for item in paid if self.rng.random() < share * 0.1]
        queue = unpaid + sample
        reviewed: list[tuple[Row, Patient]] = []
        for clerk in self.staff["reception"]:
            cap = self.rng.randint(40, 120)
            mine, queue = queue[:cap], queue[cap:]
            t = self.utc(self.env.now + self.rng.uniform(0, 15))
            for invoice, patient in mine:
                t = self.staff_access(
                    clerk, patient, [("billing", "view")], t, scenario="month_end_billing"
                )
                if invoice["status"] == "issued":
                    invoice["status"] = "paid"
                    invoice["updated_at"] = t
                    self._payment(invoice, clerk, t)
            reviewed += mine
        admin = self.staff["clinic_admin"]
        if admin and reviewed:
            boss = self.rng.choice(admin)
            t = self.utc(self.env.now + 30)
            for _, patient in reviewed[: max(1, len(reviewed) // 10)]:
                t = self.staff_access(
                    boss, patient, [("billing", "export")], t, scenario="month_end_billing"
                )

    def staff_self_view(
        self, patient: Patient, day: date, session: Session
    ) -> Generator[simpy.Event, Any, None]:
        member = next(
            (s for role in ACTIVE_ROLES for s in self.staff[role] if s.user_id == patient.user_id),
            None,
        )
        if member is None:
            return
        at = self.rng.uniform(self.minutes(day, session.start), self.minutes(day, session.end))
        yield self.env.timeout(max(0.0, at - self.env.now))
        own = {
            "doctor": [("notes", "view"), ("lab", "view")],
            "nurse": [("vitals", "view")],
            "reception": [("billing", "view")],
            "lab_tech": [("lab", "view")],
        }[member.role]
        self.staff_access(member, patient, [("demographics", "view"), *own], self.utc(self.env.now))

    def _camp(self, day: date, session: Session) -> None:
        """Health camp: reception registers many new people in one morning."""
        start, end = self.minutes(day, session.start), self.minutes(day, session.end)
        clerk = self.rng.choice(self.staff["reception"])
        for _ in range(self.rng.randint(15, 40)):
            at = self.utc(self.rng.uniform(start, end))
            patient = self.new_patient(at, clerk, walk_in_day=day)
            self.staff_access(
                clerk, patient, [("demographics", "create"), ("consent", "create")], at
            )

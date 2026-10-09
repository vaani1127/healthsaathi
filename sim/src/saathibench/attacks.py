"""Insider attack campaigns (SPEC 7, types 1 to 10).

Each campaign has one actor (a staff member of the clinic, or someone using their account) and a
mimicry level m in [0, 1]. At m = 0 the attack keeps its natural shape (off-hours, bursts, many
patients); as m grows its timing moves toward the actor's own usual hours and its volume shrinks
and spreads out, so it looks more like the actor's normal work.

Attack accesses go through the same `access()` as everything else and land in access_events;
only labels/access_labels and labels/campaigns say they are attacks. Evidence an attacker forges
(appointments, lab orders, care-team rows, break-glass events, note versions, devices) is written
to the normal tables, as it would be in the product.
"""

import json
import statistics
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import simpy

from saathibench.config import NO_SHOW_MARKED_AFTER

if TYPE_CHECKING:
    from saathibench.clinic import ClinicSim, Patient, Staff

ATTACK_NAMES = {
    1: "vip_relative_snooping",
    2: "colleague_neighbour_snooping",
    3: "role_overreach",
    4: "post_care_access",
    5: "fast_bulk_export",
    6: "low_and_slow_exfiltration",
    7: "credential_sharing",
    8: "break_glass_abuse",
    9: "edit_after_sign",
    10: "explanation_forgery",
}

# What each role looks at when snooping (allowed by the policy without break-glass).
VIEWS = {
    "doctor": [("demographics", "view"), ("notes", "view"), ("prescriptions", "view"),
               ("lab", "view"), ("vitals", "view")],
    "nurse": [("demographics", "view"), ("vitals", "view"), ("allergies", "view")],
    "reception": [("demographics", "view"), ("billing", "view")],
    "lab_tech": [("demographics", "view"), ("lab", "view")],
    "clinic_admin": [("demographics", "view"), ("billing", "view")],
}  # fmt: skip
# Off-hours an attacker prefers at m = 0, as minutes after local midnight.
OFF_HOURS = (22 * 60 + 30, 23 * 60 + 15, 6 * 60 + 30, 14 * 60 + 30)
POST_CARE_AFTER = timedelta(days=30)
Gen = Generator[simpy.Event, Any, None]


@dataclass(frozen=True)
class AttackTag:
    attack_type: int
    campaign_id: str
    mimicry: float


def lerp(a: float, b: float, m: float) -> float:
    return a + (b - a) * m


class Campaign:
    def __init__(self, sim: "ClinicSim", attack_type: int, mimicry: float, day: date) -> None:
        self.sim = sim
        self.type = attack_type
        self.m = mimicry
        self.day = day
        self.rng = sim.rng
        self.started: datetime | None = None
        self.targets: set[str] = set()
        self.tag: AttackTag | None = None
        self.actor: Staff | None = None
        self.variant = "default"

    # Helpers ----------------------------------------------------------------------------------

    def n(self, at_m0: float, at_m1: float) -> int:
        return max(1, round(lerp(at_m0, at_m1, self.m) * self.rng.uniform(0.75, 1.25)))

    def usual_minute(self, actor: "Staff") -> float:
        samples = self.sim.hours.get(actor.user_id)
        if samples:
            return statistics.median(samples)
        sessions = self.sim.p.weekday
        return (sessions[0].start.hour * 60 + sessions[-1].end.hour * 60) / 2

    def when(self, actor: "Staff", day: date) -> float:
        """A time on `day`: off-hours at m = 0, the actor's usual time at m = 1."""
        off = self.rng.choice(OFF_HOURS)
        minute = lerp(off, self.usual_minute(actor), self.m) + self.rng.gauss(0, 30)
        return self.sim.minutes(day, time(0)) + min(max(minute, 0.0), 1439.0)

    def open_days(self, count: int, within: int) -> list[date]:
        """`count` distinct clinic days from the campaign day on, spread over `within` days."""
        days = [
            self.day + timedelta(days=i)
            for i in range(max(within, count))
            if self.sim.p.sessions_on((self.day + timedelta(days=i)).weekday())
        ]
        chosen = sorted(self.rng.sample(days, min(count, len(days)))) if days else [self.day]
        return [d for d in chosen if (d - self.sim.cfg.start_date).days < self.sim.cfg.days]

    def wait_until(self, minute: float) -> Generator[simpy.Event, Any, bool]:
        if minute >= self.sim.cfg.days * 1440:
            return False
        yield self.sim.env.timeout(max(0.0, minute - self.sim.env.now))
        return True

    def hit(
        self,
        patient: "Patient",
        items: list[tuple[str, str]],
        *,
        gap: tuple[float, float] | None = None,
        device: str | None = None,
        ip_hash: str | None = None,
        break_glass_id: str | None = None,
    ) -> datetime:
        assert self.actor is not None and self.tag is not None
        at = self.sim.utc(self.sim.env.now)
        if self.started is None:
            self.started = at
        self.targets.add(patient.id)
        gap = gap or (lerp(5, 25, self.m), lerp(30, 90, self.m))
        return self.sim.staff_access(
            self.actor,
            patient,
            items,
            at,
            attack=self.tag,
            device=device,
            ip_hash=ip_hash,
            break_glass_id=break_glass_id,
            gap=gap,
        )

    def views(self, role: str, k: int | None = None) -> list[tuple[str, str]]:
        pool = VIEWS[role]
        k = k or self.rng.randint(1, len(pool))
        return [pool[0], *self.rng.sample(pool[1:], min(k, len(pool)) - 1)] if k > 1 else [pool[0]]

    def staff_of(self, *roles: str) -> list["Staff"]:
        return [s for r in roles for s in self.sim.staff[r]]

    def visited(self, n: int) -> list["Patient"]:
        seen = [p for p in self.sim._sample(n * 6) if p.visits > 0 and not p.staff_user]
        return seen[:n]

    # Campaign start ---------------------------------------------------------------------------

    def run(self) -> Gen:
        ok = yield from self.wait_until(self.sim.minutes(self.day, time(0)))
        if not ok:
            return
        planner = PLANS[self.type]
        setup = planner(self)
        # Some attacks need history (former patients, signed notes); try again a week later.
        while setup is None:
            self.day += timedelta(days=7)
            ok = yield from self.wait_until(self.sim.minutes(self.day, time(0)))
            if not ok:
                return
            setup = planner(self)
        self.tag = AttackTag(self.type, self.sim.ids.new(self.sim.utc(self.sim.env.now)), self.m)
        yield from setup()
        if self.started is not None and self.actor is not None:
            self.sim.rec.add(
                "campaigns",
                {
                    "campaign_id": self.tag.campaign_id,
                    "clinic_id": self.sim.clinic_id,
                    "attack_type": self.type,
                    "attack_name": ATTACK_NAMES[self.type],
                    "actor_user_id": self.actor.user_id,
                    "actor_role": self.actor.role,
                    "mimicry": self.m,
                    "variant": self.variant,
                    "start_at": self.started,
                    "targets": len(self.targets),
                },
            )

    # Shared shapes ----------------------------------------------------------------------------

    def visiting(
        self, day: date, fits: Callable[["Patient"], bool] | None = None
    ) -> list["Patient"]:
        """Patients with a benign appointment on `day` (a sample)."""
        return [
            p
            for p in self.sim._sample(300)
            if day in p.busy_days and not p.staff_user and (fits is None or fits(p))
        ]

    def incidental(
        self, patient: "Patient", day: date, fits: Callable[["Patient"], bool] | None = None
    ) -> "Patient":
        """With probability incidental_share, someone visiting today (who also fits) instead."""
        if self.rng.random() >= self.sim.cfg.attacks.incidental_share:
            return patient
        found = self.visiting(day, fits)
        return self.rng.choice(found) if found else patient

    def snoop(
        self,
        targets: list["Patient"],
        days: int,
        within: int,
        swap: Callable[["Patient"], bool] | None = None,
    ) -> Gen:
        """`swap` marks campaigns whose targets are not specific people: such a target may be
        replaced, for one day, by a fitting patient who is visiting that day (incidental_share)."""
        assert self.actor is not None
        for day in self.open_days(days, within):
            ok = yield from self.wait_until(self.when(self.actor, day))
            if not ok:
                return
            for patient in targets:
                if self.rng.random() < 0.8:
                    if swap is not None:
                        patient = self.incidental(patient, day, swap)
                    self.hit(patient, self.views(self.actor.role))
                    yield self.sim.env.timeout(self.rng.uniform(0.5, lerp(3, 25, self.m)))


Plan = Callable[[Campaign], Callable[[], Gen] | None]


def plan_relative(c: Campaign) -> Callable[[], Gen] | None:
    sim = c.sim
    with_relatives = [s for s in c.staff_of("doctor", "nurse", "reception", "lab_tech")
                      if sim.relatives.get(s.user_id)]  # fmt: skip
    if with_relatives:
        c.actor = c.rng.choice(with_relatives)
        targets = sim.relatives[c.actor.user_id]
        c.variant = "relative"
    else:
        c.actor = c.rng.choice(c.staff_of("doctor", "nurse", "reception", "lab_tech"))
        targets = c.visited(c.rng.randint(1, 2))
        c.variant = "vip"
        return lambda: c.snoop(targets, c.n(2, 4), 21, swap=lambda p: True)
    return lambda: c.snoop(targets, c.n(2, 4), 21)


def plan_colleague(c: Campaign) -> Callable[[], Gen] | None:
    sim = c.sim
    c.actor = c.rng.choice(c.staff_of("doctor", "nurse", "reception", "lab_tech"))
    actor = c.actor
    colleagues = [p for p in sim.staff_patients if p.user_id != actor.user_id]
    neighbours = [
        p
        for p in sim._sample(400)
        if p.ward == actor.ward and p.surname != actor.surname and not p.staff_user
    ]
    if colleagues and (not neighbours or c.rng.random() < 0.5):
        c.variant = "colleague"
        targets = c.rng.sample(colleagues, min(len(colleagues), c.rng.randint(1, 2)))
    elif neighbours:
        c.variant = "neighbour"
        targets = neighbours[: c.rng.randint(1, 3)]
        ward, surname = actor.ward, actor.surname
        return lambda: c.snoop(
            targets, c.n(2, 4), 21, swap=lambda p: p.ward == ward and p.surname != surname
        )
    else:
        return None
    return lambda: c.snoop(targets, c.n(2, 4), 21)


def plan_overreach(c: Campaign) -> Callable[[], Gen] | None:
    c.actor = c.rng.choice(c.staff_of("nurse", "reception", "lab_tech", "doctor"))
    actor = c.actor
    c.variant = actor.role
    extra = {
        "nurse": [("notes", "view"), ("prescriptions", "view")],
        "reception": [("notes", "view"), ("prescriptions", "view"), ("lab", "view")],
        "lab_tech": [("vitals", "view"), ("notes", "view")],
        "doctor": [],
    }[actor.role]

    def go() -> Gen:
        targets = c.visited(c.n(15, 4))
        days = c.open_days(c.n(1, 6), 14)
        for i, patient in enumerate(targets):
            if i % max(1, len(targets) // max(1, len(days))) == 0 and days:
                ok = yield from c.wait_until(c.when(actor, days.pop(0)))
                if not ok:
                    return
            items = c.views(actor.role)
            # Reaching for records the role cannot open (refused by the policy) gets rarer
            # as the attacker gets more careful.
            if extra and c.rng.random() < 1 - c.m:
                items.append(c.rng.choice(extra))
            c.hit(patient, items)
            yield c.sim.env.timeout(c.rng.uniform(0.5, lerp(2, 20, c.m)))

    return go


def plan_post_care(c: Campaign) -> Callable[[], Gen] | None:
    sim = c.sim
    now = sim.utc(sim.env.now)
    candidates = []
    for actor in c.staff_of("doctor", "nurse"):
        old = {p.id: p for p, t in sim.treated.get(actor.user_id, []) if now - t > POST_CARE_AFTER}
        recent = {p.id for p, t in sim.treated.get(actor.user_id, []) if now - t <= POST_CARE_AFTER}
        former = [p for pid, p in old.items() if pid not in recent and p.next_due is None]
        if former:
            candidates.append((actor, former))
    if not candidates:
        return None
    c.actor, former = c.rng.choice(candidates)
    targets = c.rng.sample(former, min(len(former), c.n(8, 2)))
    return lambda: c.snoop(targets, c.n(1, 5), 21)


def plan_bulk_export(c: Campaign) -> Callable[[], Gen] | None:
    c.actor = c.rng.choice(c.staff_of("doctor", "reception", "clinic_admin"))
    actor = c.actor
    items = (
        [("notes", "export"), ("prescriptions", "export")]
        if actor.role == "doctor"
        else [("billing", "export")]
    )
    c.variant = actor.role

    def go() -> Gen:
        own = [p for p, _ in c.sim.treated.get(actor.user_id, [])]
        pool = list({p.id: p for p in own}.values()) + c.visited(60)
        targets = c.rng.sample(pool, min(len(pool), c.n(80, 20)))
        days = c.open_days(c.n(1, 5), 10)
        per_day = max(1, -(-len(targets) // max(1, len(days))))
        for day in days:
            ok = yield from c.wait_until(c.when(actor, day))
            if not ok:
                return
            for patient in targets[:per_day]:
                c.hit(patient, items, gap=(lerp(1, 20, c.m), lerp(5, 60, c.m)))
                yield c.sim.env.timeout(c.rng.uniform(0.05, lerp(0.3, 6, c.m)))
            targets = targets[per_day:]

    return go


def plan_low_and_slow(c: Campaign) -> Callable[[], Gen] | None:
    c.actor = c.rng.choice(c.staff_of("doctor", "nurse", "reception", "lab_tech"))
    actor = c.actor

    def go() -> Gen:
        for day in c.open_days(c.rng.randint(15, 40), 60):
            ok = yield from c.wait_until(c.when(actor, day))
            if not ok:
                return
            for patient in c.visited(c.sim.poisson(lerp(3, 1, c.m)) or 1):
                patient = c.incidental(patient, day)
                items = c.views(actor.role, 2)
                if actor.role == "doctor" and c.rng.random() < 0.3 * (1 - c.m):
                    items.append(("notes", "print"))
                c.hit(patient, items)
                yield c.sim.env.timeout(c.rng.uniform(5, 60))

    return go


def plan_credential_sharing(c: Campaign) -> Callable[[], Gen] | None:
    c.actor = c.rng.choice(c.staff_of("doctor", "nurse", "reception", "lab_tech"))
    actor = c.actor

    def go() -> Gen:
        days = c.open_days(c.n(1, 3), 7)
        targets = c.visited(c.n(30, 6))
        per_day = max(1, -(-len(targets) // max(1, len(days))))
        device = None
        ip = c.sim.ids.hex()
        for day in days:
            ok = yield from c.wait_until(c.when(actor, day))
            if not ok:
                return
            if device is None:
                # Someone else's laptop or phone, seen for the first time now.
                device = c.sim._device(actor.user_id, "desk", c.sim.utc(c.sim.env.now))
            for patient in targets[:per_day]:
                items = c.views(actor.role)
                if c.rng.random() < 0.3 * (1 - c.m) and actor.role == "doctor":
                    items.append(("prescriptions", "print"))
                c.hit(patient, items, device=device, ip_hash=ip)
                yield c.sim.env.timeout(c.rng.uniform(0.2, lerp(2, 15, c.m)))
            targets = targets[per_day:]

    return go


def plan_break_glass(c: Campaign) -> Callable[[], Gen] | None:
    c.actor = c.rng.choice(c.staff_of("doctor", "nurse"))
    actor = c.actor
    items = [("demographics", "view"), ("notes", "view"), ("prescriptions", "view")]
    if actor.role == "doctor":
        items.append(("lab", "view"))

    def go() -> Gen:
        targets = c.visited(c.n(5, 2))
        days = c.open_days(c.n(1, 8), 21)
        for i, patient in enumerate(targets):
            day = days[min(i, len(days) - 1)] if days else c.day
            ok = yield from c.wait_until(max(c.when(actor, day), c.sim.env.now + 1))
            if not ok:
                return
            at = c.sim.utc(c.sim.env.now)
            glass = c.sim.ids.new(at)
            c.sim.rec.add(
                "break_glass_events",
                {
                    "id": glass,
                    "clinic_id": c.sim.clinic_id,
                    "user_id": actor.user_id,
                    "patient_id": patient.id,
                    "reason_code": "emergency",
                    "reason_text": "Patient unwell, urgent review",
                    "at": at,
                },
            )
            c.hit(patient, items, break_glass_id=glass)

    return go


def plan_edit_after_sign(c: Campaign) -> Callable[[], Gen] | None:
    sim = c.sim
    now = sim.utc(sim.env.now)
    doctors = {d.user_id: d for d in sim.staff["doctor"]}
    old_notes = [
        (pid, note)
        for pid, notes in sim.notes.items()
        for note in notes
        if timedelta(days=3) <= now - note[3] <= timedelta(days=30) and note[2] in doctors
    ]
    if not old_notes:
        return None
    by_author: dict[str, list[tuple[str, tuple[str, str, str, datetime]]]] = {}
    for pid, note in old_notes:
        by_author.setdefault(note[2], []).append((pid, note))
    author = c.rng.choice(sorted(by_author))
    c.actor = doctors[author]
    actor = c.actor
    chosen = c.rng.sample(by_author[author], min(len(by_author[author]), c.n(6, 2)))
    patients = {p.id: p for p in sim.patients}

    def go() -> Gen:
        for day in c.open_days(c.n(1, 4), 10):
            ok = yield from c.wait_until(c.when(actor, day))
            if not ok:
                return
            for pid, (note_id, encounter_id, _, _) in chosen[: max(1, len(chosen) // 2)]:
                patient = patients[pid]
                at = c.hit(patient, [("notes", "view"), ("notes", "edit")])
                common = {"clinic_id": sim.clinic_id, "patient_id": pid}
                sim.rec.add(
                    "clinical_notes",
                    {
                        "id": sim.ids.new(at),
                        **common,
                        "encounter_id": encounter_id,
                        "author_user_id": actor.user_id,
                        "version": 2,
                        "parent_id": note_id,
                        "signed_at": at,
                        "created_at": at,
                    },
                )
                if c.rng.random() < 0.4:
                    at = c.hit(patient, [("prescriptions", "edit")])
                    sim.rec.add(
                        "prescriptions",
                        {
                            "id": sim.ids.new(at),
                            **common,
                            "encounter_id": encounter_id,
                            "author_user_id": actor.user_id,
                            "items": json.dumps([{"drug": "Alprazolam 0.5 mg"}]),
                            "signed_at": at,
                            "version": 2,
                            "parent_id": None,
                            "created_at": at,
                        },
                    )
                yield sim.env.timeout(c.rng.uniform(1, 20))
            chosen[:] = chosen[max(1, len(chosen) // 2) :]
            if not chosen:
                return

    return go


def plan_forgery(c: Campaign) -> Callable[[], Gen] | None:
    sim = c.sim
    c.actor = c.rng.choice(c.staff_of("doctor", "doctor", "reception", "nurse"))
    c.variant = {
        "doctor": c.rng.choice(("appointment", "lab_order", "care_team")),
        "reception": "appointment",
        "nurse": "care_team",
    }[c.actor.role]
    if c.variant == "lab_order":
        # A lab order only explains a lab technician's access (T_LAB), so the forger is one.
        if not sim.staff["lab_tech"]:
            return None
        c.actor = c.rng.choice(sim.staff["lab_tech"])
    actor = c.actor

    def forge(patient: "Patient") -> None:
        """Create the evidence that makes the coming access look expected."""
        now = sim.env.now
        # A careful attacker creates the evidence well before using it.
        created = now - lerp(c.rng.uniform(2, 15), c.rng.uniform(90, 1200), c.m)
        at = sim.utc(created)
        if c.variant == "appointment":
            doctor = actor if actor.role == "doctor" else c.rng.choice(sim.staff["doctor"])
            # The slot is soon after the access, so the appointment explains it.
            slot = now + c.rng.uniform(15, 30)
            appt = sim._appointment(
                patient,
                doctor,
                slot,
                "followup",
                "doctor" if actor.role == "doctor" else "reception",
                actor.user_id,
                created,
            )
            # Resolved like any other appointment: the patient never comes, so reception marks a
            # no-show at the usual time. With probability m the forger cancels it instead, after
            # the access and before the slot.
            if c.rng.random() < c.m:
                when = sim.utc(c.rng.uniform(now + 9, slot - 1))
                sim.set_status("appointment", appt, "cancelled", when)
            else:
                sim.set_status("appointment", appt, "no_show", sim.utc(slot + NO_SHOW_MARKED_AFTER))
        elif c.variant == "lab_order":
            order = sim.rec.add(
                "lab_orders",
                {
                    "id": sim.ids.new(at),
                    "clinic_id": sim.clinic_id,
                    "patient_id": patient.id,
                    "encounter_id": None,
                    "ordered_by": actor.user_id,
                    "tests": json.dumps(["CBC"]),
                    "status": "ordered",
                    "created_at": at,
                    "updated_at": at,
                },
            )
            sim.status_event("lab_order", order, at)
            # No sample is ever taken. With probability m the forger cancels the order soon
            # after the access; otherwise it is cancelled at closing time like any order whose
            # sample was never collected.
            if c.rng.random() < c.m:
                sim.set_status("lab_order", order, "cancelled", sim.utc(now + c.rng.uniform(9, 30)))
            else:
                close = sim.close_of_day(now)
                sim.set_status("lab_order", order, "cancelled", sim.utc(close))
        else:
            sim.rec.add(
                "care_team_assignments",
                {
                    "id": sim.ids.new(at),
                    "clinic_id": sim.clinic_id,
                    "patient_id": patient.id,
                    "user_id": actor.user_id,
                    "role": actor.role,
                    "starts_at": at,
                    "ends_at": None,
                    "status": "active",
                    "created_by": actor.user_id,
                    "created_at": at,
                    "updated_at": at,
                },
            )

    def go() -> Gen:
        targets = c.visited(c.n(4, 1))
        days = c.open_days(len(targets), 21)
        for patient, day in zip(targets, days, strict=False):
            ok = yield from c.wait_until(c.when(actor, day))
            if not ok:
                return
            forge(patient)
            c.hit(patient, c.views(actor.role))

    return go


PLANS: dict[int, Plan] = {
    1: plan_relative,
    2: plan_colleague,
    3: plan_overreach,
    4: plan_post_care,
    5: plan_bulk_export,
    6: plan_low_and_slow,
    7: plan_credential_sharing,
    8: plan_break_glass,
    9: plan_edit_after_sign,
    10: plan_forgery,
}


def plan_campaigns(sim: "ClinicSim") -> None:
    """Start the clinic's campaigns as SimPy processes. Drawn from the clinic's own generator, so
    they are reproducible."""
    cfg = sim.cfg.attacks
    if cfg.campaigns_per_clinic_month <= 0 or cfg.first_day >= sim.cfg.days:
        return
    count = sim.poisson(cfg.campaigns_per_clinic_month * sim.cfg.days / 30)
    for _ in range(count):
        attack_type = sim.rng.choice(cfg.types)
        mimicry = sim.rng.random() if cfg.mimicry is None else sim.rng.choice(cfg.mimicry)
        day = sim.cfg.start_date + timedelta(days=sim.rng.randint(cfg.first_day, sim.cfg.days - 1))
        sim.env.process(Campaign(sim, attack_type, round(mimicry, 4), day).run())

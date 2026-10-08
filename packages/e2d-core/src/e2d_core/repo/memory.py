"""Repository backend over in-memory polars frames.

Frames use the product table and column names (SPEC 3.1), so simulator output can be loaded
directly. Datetime columns must be timezone-aware UTC. Id columns may hold UUIDs or strings.
Missing tables are treated as empty.
"""

import dataclasses
import uuid
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from e2d_core.explain.model import (
    AppointmentEv,
    BreakGlassEv,
    CareTeamEv,
    ClinicContext,
    EncounterEv,
    EvidenceBundle,
    EvidenceQuery,
    InvoiceEv,
    LabOrderEv,
    OpenHours,
    PatientEv,
    ProgressEv,
    QueueTokenEv,
    ReferralEv,
    ShiftEv,
    UserContext,
)
from e2d_core.repo import (
    APPOINTMENT_LOOKAHEAD,
    APPOINTMENT_LOOKBACK,
    ASSIGNMENT_GRACE,
    BOOKING_NORM_MIN,
    BOOKING_NORM_WINDOW,
    CREATION_WINDOW,
    ENCOUNTER_LOOKBACK,
    INVOICE_LOOKBACK,
    LAB_LOOKBACK,
    PROGRESS_LOOKBACK,
    WINDOW_MARGIN,
    median,
)

COLUMNS: dict[str, tuple[str, ...]] = {
    "patients": ("id", "clinic_id", "user_id", "created_by", "created_at"),
    "appointments": (
        "id",
        "clinic_id",
        "patient_id",
        "doctor_user_id",
        "slot_start",
        "slot_end",
        "kind",
        "status",
        "source",
        "created_by",
        "created_at",
        "updated_at",
    ),
    "queue_tokens": (
        "id",
        "clinic_id",
        "appointment_id",
        "token_no",
        "day",
        "status",
        "assigned_nurse_user_id",
        "created_by",
        "created_at",
    ),
    "shifts": ("id", "clinic_id", "user_id", "role", "starts_at", "ends_at", "status"),
    "invoices": (
        "id",
        "clinic_id",
        "patient_id",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    ),
    "encounters": (
        "id",
        "clinic_id",
        "patient_id",
        "doctor_user_id",
        "started_at",
        "created_by",
        "created_at",
    ),
    "lab_orders": (
        "id",
        "clinic_id",
        "patient_id",
        "ordered_by",
        "status",
        "created_at",
        "updated_at",
    ),
    "lab_results": ("id", "clinic_id", "lab_order_id", "patient_id", "resulted_at", "released_at"),
    "referrals": (
        "id",
        "clinic_id",
        "patient_id",
        "from_user_id",
        "to_user_id",
        "valid_until",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    ),
    "care_team_assignments": (
        "id",
        "clinic_id",
        "patient_id",
        "user_id",
        "role",
        "starts_at",
        "ends_at",
        "status",
        "created_by",
        "created_at",
    ),
    "break_glass_events": ("id", "clinic_id", "user_id", "patient_id", "reason_code", "at"),
    "vitals": ("id", "clinic_id", "patient_id", "recorded_at"),
    "clinical_notes": ("id", "clinic_id", "patient_id", "created_at"),
    "prescriptions": ("id", "clinic_id", "patient_id", "created_at"),
    "schedules": ("id", "clinic_id", "weekday", "start_time", "end_time", "status"),
    "memberships": ("id", "clinic_id", "user_id", "role", "is_active"),
}

DATETIME = pl.Datetime("us", "UTC")
_TYPES: dict[str, pl.DataType] = {
    "created_at": DATETIME,
    "updated_at": DATETIME,
    "slot_start": DATETIME,
    "slot_end": DATETIME,
    "starts_at": DATETIME,
    "ends_at": DATETIME,
    "started_at": DATETIME,
    "valid_until": DATETIME,
    "resulted_at": DATETIME,
    "released_at": DATETIME,
    "recorded_at": DATETIME,
    "at": DATETIME,
    "day": pl.Date(),
    "start_time": pl.Time(),
    "end_time": pl.Time(),
    "token_no": pl.Int64(),
    "weekday": pl.Int64(),
    "is_active": pl.Boolean(),
}


def _is_id_column(name: str) -> bool:
    return name == "id" or name.endswith("_id") or name in ("created_by", "ordered_by")


def _dtype(column: str) -> pl.DataType:
    return pl.String() if _is_id_column(column) else _TYPES.get(column, pl.String())


Rec = dict[str, Any]

# Tables looked up per patient, and the column each one is sorted and windowed on.
_PATIENT_TABLES: dict[str, str] = {
    "appointments": "slot_start",
    "invoices": "created_at",
    "encounters": "started_at",
    "lab_orders": "created_at",
    "referrals": "created_at",
    "care_team_assignments": "starts_at",
    "break_glass_events": "at",
    "vitals": "recorded_at",
    "clinical_notes": "created_at",
    "prescriptions": "created_at",
    "lab_results": "resulted_at",
}
_PROGRESS = (
    ("vitals", "vitals", "recorded_at"),
    ("note", "clinical_notes", "created_at"),
    ("prescription", "prescriptions", "created_at"),
    ("lab_result", "lab_results", "resulted_at"),
    ("invoice", "invoices", "created_at"),
)
_CREATORS = (
    ("appointments", "created_by"),
    ("lab_orders", "ordered_by"),
    ("referrals", "created_by"),
    ("care_team_assignments", "created_by"),
)


def _between(value: Any, start: Any, end: Any) -> bool:
    """polars is_between with closed="both": null is never inside."""
    return value is not None and start <= value <= end


def _build[T](cls: type[T], row: Rec) -> T:
    names = [f.name for f in dataclasses.fields(cls)]  # type: ignore[arg-type]
    return cls(**{n: row[n] for n in names})


class _Sorted:
    """Rows sorted by one column (rows where it is null are dropped), with keys for bisecting."""

    __slots__ = ("keys", "rows")

    def __init__(self, rows: list[Rec], column: str) -> None:
        self.rows = sorted(
            (r for r in rows if r[column] is not None), key=lambda r: (r[column], r["id"])
        )
        self.keys = [r[column] for r in self.rows]

    def window(self, start: Any, end: Any) -> list[Rec]:
        return self.rows[bisect_left(self.keys, start) : bisect_right(self.keys, end)]


_EMPTY = _Sorted([], "id")
# A lab order without a result still appears once (a left join).
_NO_RESULT: list[Rec | None] = [None]


class MemoryRepository:
    """Reads the frames once into per-clinic, per-patient and per-user indexes, then answers each
    query with binary searches. The filters and ordering are the same as the SQL backend's; the
    shared backend tests check this."""

    def __init__(self, frames: Mapping[str, pl.DataFrame]) -> None:
        self.frames: dict[str, pl.DataFrame] = {}
        for table, columns in COLUMNS.items():
            frame = frames.get(table)
            if frame is None:
                frame = pl.DataFrame(schema={c: _dtype(c) for c in columns})
            missing = set(columns) - set(frame.columns)
            if missing:
                raise ValueError(f"{table} is missing columns {sorted(missing)}")
            self.frames[table] = frame.with_columns(
                [pl.col(c).cast(pl.String) for c in columns if _is_id_column(c)]
            )
        self._rows: dict[str, list[Rec]] = {}
        self._by_patient: dict[str, dict[tuple[uuid.UUID, uuid.UUID], _Sorted]] = {}
        self._shifts: dict[tuple[uuid.UUID, uuid.UUID], _Sorted] | None = None
        self._tokens: dict[tuple[uuid.UUID, uuid.UUID], list[Rec]] | None = None
        self._results: dict[uuid.UUID, list[Rec]] | None = None
        self._patients: dict[tuple[uuid.UUID, uuid.UUID], Rec] | None = None
        self._hours: dict[uuid.UUID, tuple[OpenHours, ...]] = {}
        self._bookings: dict[uuid.UUID, tuple[list[Any], list[int]]] = {}
        self._members: dict[tuple[uuid.UUID, str], list[uuid.UUID]] = {}
        self._created: dict[uuid.UUID, tuple[list[Any], list[uuid.UUID]]] = {}

    # Indexes, built on first use --------------------------------------------------------------

    def _table(self, table: str) -> list[Rec]:
        rows = self._rows.get(table)
        if rows is None:
            frame = self.frames[table].select(COLUMNS[table])
            id_columns = [c for c in COLUMNS[table] if _is_id_column(c)]
            rows = frame.to_dicts()
            for row in rows:
                for c in id_columns:
                    if row[c] is not None:
                        row[c] = uuid.UUID(row[c])
            self._rows[table] = rows
        return rows

    def _patient_rows(self, table: str, clinic: uuid.UUID, patient: uuid.UUID) -> _Sorted:
        index = self._by_patient.get(table)
        if index is None:
            groups: dict[tuple[uuid.UUID, uuid.UUID], list[Rec]] = defaultdict(list)
            for row in self._table(table):
                groups[(row["clinic_id"], row["patient_id"])].append(row)
            column = _PATIENT_TABLES[table]
            index = {k: _Sorted(v, column) for k, v in groups.items()}
            self._by_patient[table] = index
        return index.get((clinic, patient)) or _EMPTY

    def _patient(self, clinic: uuid.UUID, patient: uuid.UUID) -> Rec | None:
        if self._patients is None:
            self._patients = {(r["clinic_id"], r["id"]): r for r in self._table("patients")}
        return self._patients.get((clinic, patient))

    def _user_shifts(self, clinic: uuid.UUID, user: uuid.UUID) -> _Sorted:
        if self._shifts is None:
            groups: dict[tuple[uuid.UUID, uuid.UUID], list[Rec]] = defaultdict(list)
            for row in self._table("shifts"):
                groups[(row["clinic_id"], row["user_id"])].append(row)
            self._shifts = {k: _Sorted(v, "starts_at") for k, v in groups.items()}
        return self._shifts.get((clinic, user)) or _EMPTY

    def _tokens_of(self, clinic: uuid.UUID, appointment: uuid.UUID) -> list[Rec]:
        if self._tokens is None:
            self._tokens = defaultdict(list)
            for row in self._table("queue_tokens"):
                self._tokens[(row["clinic_id"], row["appointment_id"])].append(row)
        return self._tokens.get((clinic, appointment), [])

    def _results_of(self, order: uuid.UUID) -> list[Rec]:
        if self._results is None:
            self._results = defaultdict(list)
            for row in self._table("lab_results"):
                self._results[row["lab_order_id"]].append(row)
        return self._results.get(order, [])

    # Queries ----------------------------------------------------------------------------------

    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle:
        return self.evidence_for_sync(query)

    def evidence_for_sync(self, query: EvidenceQuery) -> EvidenceBundle:
        clinic, patient, user = query.clinic_id, query.patient_id, query.user_id
        at = query.at
        as_of = max(query.as_of or at, at)
        local_day = at.astimezone(ZoneInfo(query.timezone)).date()

        own_appointments = self._patient_rows("appointments", clinic, patient)
        appointments = own_appointments.window(
            at - APPOINTMENT_LOOKBACK, at + APPOINTMENT_LOOKAHEAD
        )
        first, last = local_day - timedelta(days=1), local_day + timedelta(days=1)
        tokens = sorted(
            (
                {**token, "patient_id": patient}
                for appt in own_appointments.rows
                for token in self._tokens_of(clinic, appt["id"])
                if _between(token["day"], first, last)
            ),
            key=lambda r: (r["day"], r["token_no"]),
        )
        shifts = [
            s
            for s in self._user_shifts(clinic, user).rows
            if s["starts_at"] <= at + WINDOW_MARGIN
            and s["ends_at"] is not None
            and s["ends_at"] >= at - WINDOW_MARGIN
        ]
        invoices = self._patient_rows("invoices", clinic, patient).window(
            at - INVOICE_LOOKBACK, at + WINDOW_MARGIN
        )
        encounters = self._patient_rows("encounters", clinic, patient).window(
            at - ENCOUNTER_LOOKBACK, at + WINDOW_MARGIN
        )
        lab_orders = [
            {**order, "released_at": result["released_at"] if result else None}
            for order in self._patient_rows("lab_orders", clinic, patient).window(
                at - LAB_LOOKBACK, at + WINDOW_MARGIN
            )
            for result in (self._results_of(order["id"]) or _NO_RESULT)
        ]
        start, end = at - ASSIGNMENT_GRACE, at + WINDOW_MARGIN
        referrals = [
            r
            for r in self._patient_rows("referrals", clinic, patient).rows
            if r["to_user_id"] == user
            and r["created_at"] <= end
            and r["valid_until"] is not None
            and r["valid_until"] >= start
        ]
        care_team = [
            c
            for c in self._patient_rows("care_team_assignments", clinic, patient).rows
            if c["user_id"] == user
            and c["starts_at"] <= end
            and (c["ends_at"] is None or c["ends_at"] >= start)
        ]
        break_glass = [
            b
            for b in self._patient_rows("break_glass_events", clinic, patient).window(
                at - WINDOW_MARGIN, at + WINDOW_MARGIN
            )
            if b["user_id"] == user
        ]
        found = self._patient(clinic, patient)
        return EvidenceBundle(
            patient=_build(PatientEv, found) if found else None,
            appointments=tuple(_build(AppointmentEv, r) for r in appointments),
            queue_tokens=tuple(_build(QueueTokenEv, r) for r in tokens),
            shifts=tuple(_build(ShiftEv, r) for r in shifts),
            invoices=tuple(_build(InvoiceEv, r) for r in invoices),
            encounters=tuple(_build(EncounterEv, r) for r in encounters),
            lab_orders=tuple(_build(LabOrderEv, r) for r in lab_orders),
            referrals=tuple(_build(ReferralEv, r) for r in referrals),
            care_team=tuple(_build(CareTeamEv, r) for r in care_team),
            break_glass=tuple(_build(BreakGlassEv, r) for r in break_glass),
            progress=self._progress(clinic, patient, at - PROGRESS_LOOKBACK, as_of),
            clinic=self._clinic_context(clinic, query),
            user=self._user_context(clinic, query),
        )

    def _progress(
        self, clinic: uuid.UUID, patient: uuid.UUID, start: Any, end: Any
    ) -> tuple[ProgressEv, ...]:
        rows: list[tuple[Any, str]] = []
        for kind, table, column in _PROGRESS:
            for r in self._patient_rows(table, clinic, patient).window(start, end):
                rows.append((r[column], kind))
        return tuple(ProgressEv(kind, t) for t, kind in sorted(rows))

    def _clinic_context(self, clinic: uuid.UUID, query: EvidenceQuery) -> ClinicContext:
        hours = self._hours.get(clinic)
        if hours is None:
            unique = {
                (r["weekday"], r["start_time"], r["end_time"])
                for r in self._table("schedules")
                if r["clinic_id"] == clinic and r["status"] == "active"
            }
            hours = tuple(OpenHours(*h) for h in sorted(unique))
            self._hours[clinic] = hours
        bookings = self._bookings.get(clinic)
        if bookings is None:
            made = sorted(
                (r["created_at"], r["source"] == "reception")
                for r in self._table("appointments")
                if r["clinic_id"] == clinic and r["created_at"] is not None
            )
            prefix = [0]
            for _, by_reception in made:
                prefix.append(prefix[-1] + int(by_reception))
            bookings = ([t for t, _ in made], prefix)
            self._bookings[clinic] = bookings
        times, prefix = bookings
        lo = bisect_left(times, query.at - BOOKING_NORM_WINDOW)
        hi = bisect_right(times, query.at)
        total = hi - lo
        by_reception = prefix[hi] - prefix[lo]
        return ClinicContext(
            hours=hours,
            reception_booking_share=by_reception / total if total >= BOOKING_NORM_MIN else None,
        )

    def _user_context(self, clinic: uuid.UUID, query: EvidenceQuery) -> UserContext:
        if query.role is None:
            return UserContext()
        members = self._members.get((clinic, query.role))
        if members is None:
            members = sorted(
                {
                    r["user_id"]
                    for r in self._table("memberships")
                    if r["clinic_id"] == clinic and r["role"] == query.role and r["is_active"]
                }
            )
            self._members[(clinic, query.role)] = members
        created = self._created.get(clinic)
        if created is None:
            made = sorted(
                (
                    (r["created_at"], r[column])
                    for table, column in _CREATORS
                    for r in self._table(table)
                    if r["clinic_id"] == clinic and r["created_at"] is not None
                ),
                key=lambda pair: pair[0],
            )
            created = ([t for t, _ in made], [u for _, u in made])
            self._created[clinic] = created
        times, creators = created
        lo = bisect_left(times, query.at - CREATION_WINDOW)
        hi = bisect_right(times, query.at)
        counts = Counter(creators[lo:hi])
        by_user = {m: counts.get(m, 0) for m in members}
        return UserContext(
            created_7d=int(by_user.get(query.user_id, 0)),
            role_median_7d=median([int(v) for v in by_user.values()]),
        )


def frames_from_rows(rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, pl.DataFrame]:
    """Build typed frames from row dicts (ids may be UUIDs). Tables not given are left out."""
    frames = {}
    for table, items in rows.items():
        columns = COLUMNS[table]
        schema = {c: _dtype(c) for c in columns}
        data = [
            {c: (str(r[c]) if isinstance(r.get(c), uuid.UUID) else r.get(c)) for c in columns}
            for r in items
        ]
        frames[table] = (
            pl.DataFrame(data, schema=schema, orient="row") if data else pl.DataFrame(schema=schema)
        )
    return frames

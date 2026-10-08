"""Repository backend over in-memory polars frames.

Frames use the product table and column names (SPEC 3.1), so simulator output can be loaded
directly. Datetime columns must be timezone-aware UTC. Id columns may hold UUIDs or strings.
Missing tables are treated as empty.
"""

import dataclasses
import uuid
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


def _records[T](cls: type[T], frame: pl.DataFrame) -> tuple[T, ...]:
    """Build dataclass records from a frame, turning id columns back into UUIDs."""
    names = [f.name for f in dataclasses.fields(cls)]  # type: ignore[arg-type]
    out = []
    for row in frame.select(names).iter_rows(named=True):
        values: dict[str, Any] = {
            k: (uuid.UUID(v) if v is not None and _is_id_column(k) else v) for k, v in row.items()
        }
        out.append(cls(**values))
    return tuple(out)


class MemoryRepository:
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

    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle:
        return self.evidence_for_sync(query)

    def _clinic(self, table: str, clinic: str) -> pl.DataFrame:
        return self.frames[table].filter(pl.col("clinic_id") == clinic)

    def evidence_for_sync(self, query: EvidenceQuery) -> EvidenceBundle:
        clinic, patient, user = str(query.clinic_id), str(query.patient_id), str(query.user_id)
        at = query.at
        as_of = max(query.as_of or at, at)
        local_day = at.astimezone(ZoneInfo(query.timezone)).date()
        of_patient = pl.col("patient_id") == patient
        of_user = pl.col("user_id") == user

        patients = self._clinic("patients", clinic).filter(pl.col("id") == patient)
        appointments = (
            self._clinic("appointments", clinic)
            .filter(
                of_patient
                & pl.col("slot_start").is_between(
                    at - APPOINTMENT_LOOKBACK, at + APPOINTMENT_LOOKAHEAD
                )
            )
            .sort(["slot_start", "id"])
        )
        tokens = (
            self._clinic("queue_tokens", clinic)
            .filter(
                pl.col("day").is_between(
                    local_day - timedelta(days=1), local_day + timedelta(days=1)
                )
            )
            .join(
                self._clinic("appointments", clinic).select(
                    pl.col("id").alias("appointment_id"), "patient_id"
                ),
                on="appointment_id",
                how="inner",
            )
            .filter(of_patient)
            .sort(["day", "token_no"])
        )
        shifts = (
            self._clinic("shifts", clinic)
            .filter(
                of_user
                & (pl.col("starts_at") <= at + WINDOW_MARGIN)
                & (pl.col("ends_at") >= at - WINDOW_MARGIN)
            )
            .sort(["starts_at", "id"])
        )
        invoices = (
            self._clinic("invoices", clinic)
            .filter(
                of_patient
                & pl.col("created_at").is_between(at - INVOICE_LOOKBACK, at + WINDOW_MARGIN)
            )
            .sort(["created_at", "id"])
        )
        encounters = (
            self._clinic("encounters", clinic)
            .filter(
                of_patient
                & pl.col("started_at").is_between(at - ENCOUNTER_LOOKBACK, at + WINDOW_MARGIN)
            )
            .sort(["started_at", "id"])
        )
        lab_orders = (
            self._clinic("lab_orders", clinic)
            .filter(
                of_patient & pl.col("created_at").is_between(at - LAB_LOOKBACK, at + WINDOW_MARGIN)
            )
            .join(
                self._clinic("lab_results", clinic).select(
                    pl.col("lab_order_id").alias("id"), "released_at"
                ),
                on="id",
                how="left",
            )
            .sort(["created_at", "id"])
        )
        start, end = at - ASSIGNMENT_GRACE, at + WINDOW_MARGIN
        referrals = (
            self._clinic("referrals", clinic)
            .filter(
                of_patient
                & (pl.col("to_user_id") == user)
                & (pl.col("created_at") <= end)
                & (pl.col("valid_until") >= start)
            )
            .sort(["created_at", "id"])
        )
        care_team = (
            self._clinic("care_team_assignments", clinic)
            .filter(
                of_patient
                & of_user
                & (pl.col("starts_at") <= end)
                & (pl.col("ends_at").is_null() | (pl.col("ends_at") >= start))
            )
            .sort(["starts_at", "id"])
        )
        break_glass = (
            self._clinic("break_glass_events", clinic)
            .filter(
                of_patient
                & of_user
                & pl.col("at").is_between(at - WINDOW_MARGIN, at + WINDOW_MARGIN)
            )
            .sort(["at", "id"])
        )

        patient_records = _records(PatientEv, patients)
        return EvidenceBundle(
            patient=patient_records[0] if patient_records else None,
            appointments=_records(AppointmentEv, appointments),
            queue_tokens=_records(QueueTokenEv, tokens),
            shifts=_records(ShiftEv, shifts),
            invoices=_records(InvoiceEv, invoices),
            encounters=_records(EncounterEv, encounters),
            lab_orders=_records(LabOrderEv, lab_orders),
            referrals=_records(ReferralEv, referrals),
            care_team=_records(CareTeamEv, care_team),
            break_glass=_records(BreakGlassEv, break_glass),
            progress=self._progress(clinic, patient, at - PROGRESS_LOOKBACK, as_of),
            clinic=self._clinic_context(clinic, query),
            user=self._user_context(clinic, query),
        )

    def _progress(self, clinic: str, patient: str, start: Any, end: Any) -> tuple[ProgressEv, ...]:
        sources = (
            ("vitals", "vitals", "recorded_at"),
            ("note", "clinical_notes", "created_at"),
            ("prescription", "prescriptions", "created_at"),
            ("lab_result", "lab_results", "resulted_at"),
            ("invoice", "invoices", "created_at"),
        )
        rows: list[tuple[Any, str]] = []
        for kind, table, column in sources:
            frame = self._clinic(table, clinic).filter(
                (pl.col("patient_id") == patient) & pl.col(column).is_between(start, end)
            )
            rows.extend((t, kind) for t in frame[column].to_list())
        return tuple(ProgressEv(kind, t) for t, kind in sorted(rows))

    def _clinic_context(self, clinic: str, query: EvidenceQuery) -> ClinicContext:
        hours = (
            self._clinic("schedules", clinic)
            .filter(pl.col("status") == "active")
            .select("weekday", "start_time", "end_time")
            .unique()
            .sort(["weekday", "start_time", "end_time"])
        )
        recent = self._clinic("appointments", clinic).filter(
            pl.col("created_at").is_between(query.at - BOOKING_NORM_WINDOW, query.at)
        )
        total = recent.height
        by_reception = recent.filter(pl.col("source") == "reception").height
        return ClinicContext(
            hours=tuple(OpenHours(*h) for h in hours.iter_rows()),
            reception_booking_share=by_reception / total if total >= BOOKING_NORM_MIN else None,
        )

    def _user_context(self, clinic: str, query: EvidenceQuery) -> UserContext:
        if query.role is None:
            return UserContext()
        members = (
            self._clinic("memberships", clinic)
            .filter((pl.col("role") == query.role) & pl.col("is_active"))
            .select("user_id")
            .unique()
        )
        start, end = query.at - CREATION_WINDOW, query.at
        creators = []
        for table, column in (
            ("appointments", "created_by"),
            ("lab_orders", "ordered_by"),
            ("referrals", "created_by"),
            ("care_team_assignments", "created_by"),
        ):
            frame = self._clinic(table, clinic).filter(pl.col("created_at").is_between(start, end))
            creators.append(frame.select(pl.col(column).alias("user_id")))
        made = pl.concat(creators).group_by("user_id").len()
        counts = members.join(made, on="user_id", how="left").with_columns(
            pl.col("len").fill_null(0)
        )
        by_user = dict(zip(counts["user_id"].to_list(), counts["len"].to_list(), strict=True))
        return UserContext(
            created_7d=int(by_user.get(str(query.user_id), 0)),
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

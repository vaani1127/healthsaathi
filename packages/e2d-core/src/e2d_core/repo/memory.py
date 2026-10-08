"""Repository backend over in-memory polars frames.

Frames use the product table and column names (SPEC 3.1), so simulator output can be loaded
directly. Datetime columns must be timezone-aware UTC. Id columns may hold UUIDs or strings.
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
    EvidenceBundle,
    EvidenceQuery,
    InvoiceEv,
    PatientEv,
    QueueTokenEv,
    ShiftEv,
)
from e2d_core.repo import (
    APPOINTMENT_LOOKAHEAD,
    APPOINTMENT_LOOKBACK,
    INVOICE_LOOKBACK,
    SHIFT_MARGIN,
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
}


def _is_id_column(name: str) -> bool:
    return name == "id" or name.endswith("_id") or name == "created_by"


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
                frame = pl.DataFrame({c: [] for c in columns})
            missing = set(columns) - set(frame.columns)
            if missing:
                raise ValueError(f"{table} is missing columns {sorted(missing)}")
            self.frames[table] = frame.with_columns(
                [pl.col(c).cast(pl.String) for c in columns if _is_id_column(c)]
            )

    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle:
        return self.evidence_for_sync(query)

    def evidence_for_sync(self, query: EvidenceQuery) -> EvidenceBundle:
        clinic, patient, user = str(query.clinic_id), str(query.patient_id), str(query.user_id)
        at = query.at
        local_day = at.astimezone(ZoneInfo(query.timezone)).date()
        f = self.frames
        in_clinic = pl.col("clinic_id") == clinic

        patients = f["patients"].filter(in_clinic & (pl.col("id") == patient))
        appointments = (
            f["appointments"]
            .filter(
                in_clinic
                & (pl.col("patient_id") == patient)
                & pl.col("slot_start").is_between(
                    at - APPOINTMENT_LOOKBACK, at + APPOINTMENT_LOOKAHEAD
                )
            )
            .sort(["slot_start", "id"])
        )
        tokens = (
            f["queue_tokens"]
            .filter(
                in_clinic
                & pl.col("day").is_between(
                    local_day - timedelta(days=1), local_day + timedelta(days=1)
                )
            )
            .join(
                f["appointments"]
                .filter(in_clinic)
                .select(pl.col("id").alias("appointment_id"), "patient_id"),
                on="appointment_id",
                how="inner",
            )
            .filter(pl.col("patient_id") == patient)
            .sort(["day", "token_no"])
        )
        shifts = (
            f["shifts"]
            .filter(
                in_clinic
                & (pl.col("user_id") == user)
                & (pl.col("starts_at") <= at + SHIFT_MARGIN)
                & (pl.col("ends_at") >= at - SHIFT_MARGIN)
            )
            .sort(["starts_at", "id"])
        )
        invoices = (
            f["invoices"]
            .filter(
                in_clinic
                & (pl.col("patient_id") == patient)
                & pl.col("created_at").is_between(at - INVOICE_LOOKBACK, at + timedelta(days=1))
            )
            .sort(["created_at", "id"])
        )

        patient_records = _records(PatientEv, patients)
        return EvidenceBundle(
            patient=patient_records[0] if patient_records else None,
            appointments=_records(AppointmentEv, appointments),
            queue_tokens=_records(QueueTokenEv, tokens),
            shifts=_records(ShiftEv, shifts),
            invoices=_records(InvoiceEv, invoices),
        )

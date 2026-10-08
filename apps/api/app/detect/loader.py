"""Load one clinic's rows from Postgres into frames with the product's table shapes, so the same
e2d-core feature code runs here and on simulator output."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.db.models import (
    AccessEvent,
    AccessExplanation,
    Appointment,
    CareTeamAssignment,
    Device,
    Encounter,
    LabOrder,
    LabResult,
    Membership,
    Patient,
    Referral,
    Shift,
    User,
    UserSession,
    Vital,
)
from e2d_core.features import explanation_rows
from e2d_core.features.explanation import SCHEMA as EXPLANATION_SCHEMA
from e2d_core.features.explanation import flags_from_json

# Rolling behaviour windows reach back 7 days; care relations count for a year.
BEHAVIOUR_LOOKBACK = timedelta(days=7)
CARE_LOOKBACK = timedelta(days=365)
TS = pl.Datetime("us", "UTC")


def _value(v: Any) -> Any:
    return str(v) if isinstance(v, uuid.UUID) else getattr(v, "value", v)


async def _frame(db: AsyncSession, stmt: Executable, schema: dict[str, Any]) -> pl.DataFrame:
    rows = (await db.execute(stmt)).all()
    data = [{k: _value(v) for k, v in zip(schema, row, strict=True)} for row in rows]
    return pl.DataFrame(data, schema=schema, orient="row") if data else pl.DataFrame(schema=schema)


S, B = pl.String, pl.Boolean


async def load_clinic(
    db: AsyncSession, clinic_id: uuid.UUID, start: datetime, end: datetime
) -> tuple[pl.DataFrame, dict[str, pl.DataFrame], pl.DataFrame]:
    """Accesses in [start - 7 days, end) with their explanations, and the frames feature code
    needs. Runs inside a tenant session for `clinic_id`, so RLS applies."""
    since = start - BEHAVIOUR_LOOKBACK
    care_since = start - CARE_LOOKBACK
    e = AccessEvent
    events = await _frame(
        db,
        select(
            e.id, e.clinic_id, e.user_id, e.role, e.patient_id, e.resource_type, e.action, e.at,
            e.session_id, e.device_id, e.decision, e.break_glass_id, e.policy_version,
        )
        .where(e.clinic_id == clinic_id, e.at >= since, e.at < end)
        .order_by(e.at, e.id),
        {
            "id": S, "clinic_id": S, "user_id": S, "role": S, "patient_id": S,
            "resource_type": S, "action": S, "at": TS, "session_id": S, "device_id": S,
            "decision": S, "break_glass_id": S, "policy_version": S,
        },
    )  # fmt: skip
    x = AccessExplanation
    stored = (
        await db.execute(
            select(x.access_event_id, x.template_code, x.strength, x.forgery_flags).where(
                x.clinic_id == clinic_id, x.computed_at >= since - timedelta(hours=1)
            )
        )
    ).all()
    by_event = {str(r.access_event_id): r for r in stored}
    glass = dict(zip(events["id"], events["break_glass_id"].is_not_null(), strict=True))
    rows: list[dict[str, Any]] = []
    for event_id in events["id"]:
        found = by_event.get(event_id)
        rows.append(
            {
                "access_event_id": event_id,
                "template_code": found.template_code if found else None,
                "strength": float(found.strength) if found else 0.0,
                "break_glass": glass[event_id],
                **flags_from_json(found.forgery_flags if found else None),
            }
        )
    explanations = (
        pl.DataFrame(rows, schema=EXPLANATION_SCHEMA, orient="row")
        if rows
        else explanation_rows([])
    )

    users_in_events = set(events["user_id"].to_list())
    frames: dict[str, pl.DataFrame] = {
        "shifts": await _frame(
            db,
            select(Shift.user_id, Shift.starts_at, Shift.ends_at, Shift.status).where(
                Shift.clinic_id == clinic_id, Shift.ends_at >= since, Shift.starts_at < end
            ),
            {"user_id": S, "starts_at": TS, "ends_at": TS, "status": S},
        ),
        "sessions": await _frame(
            db,
            select(UserSession.id, UserSession.created_at).where(
                UserSession.id.in_(
                    [uuid.UUID(s) for s in events["session_id"].drop_nulls().unique()]
                )
            ),
            {"id": S, "created_at": TS},
        ),
        "devices": await _frame(
            db,
            select(Device.id, Device.first_seen).where(
                Device.user_id.in_([uuid.UUID(u) for u in users_in_events])
            ),
            {"id": S, "first_seen": TS},
        ),
        "patients": await _frame(
            db,
            select(Patient.id, Patient.user_id, Patient.name, Patient.address).where(
                Patient.clinic_id == clinic_id
            ),
            {"id": S, "user_id": S, "name": S, "address": S},
        ),
        "memberships": await _frame(
            db,
            select(Membership.user_id, Membership.role).where(Membership.clinic_id == clinic_id),
            {"user_id": S, "role": S},
        ),
        "users": await _frame(
            db,
            select(User.id, User.name)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.clinic_id == clinic_id)
            .distinct(),
            {"id": S, "name": S},
        ),
    }
    care = (
        ("appointments", Appointment, Appointment.doctor_user_id, Appointment.created_at),
        ("encounters", Encounter, Encounter.doctor_user_id, Encounter.started_at),
        ("referrals", Referral, Referral.to_user_id, Referral.created_at),
        ("care_team_assignments", CareTeamAssignment, CareTeamAssignment.user_id,
         CareTeamAssignment.starts_at),
        ("lab_orders", LabOrder, LabOrder.ordered_by, LabOrder.created_at),
        ("lab_results", LabResult, LabResult.resulted_by, LabResult.resulted_at),
        ("vitals", Vital, Vital.recorded_by, Vital.recorded_at),
    )  # fmt: skip
    for table, model, user_col, time_col in care:
        frames[table] = await _frame(
            db,
            select(user_col, model.patient_id, time_col).where(
                model.clinic_id == clinic_id, time_col >= care_since, time_col < end
            ),
            {user_col.key: S, "patient_id": S, time_col.key: TS},
        )
    return events, frames, explanations

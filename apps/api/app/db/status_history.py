"""Status history (status_events).

Every status an appointment, lab order, referral or invoice takes, creation included, is written
to status_events in the same flush as the change, so the explanation engine can read the status a
record had at the time of an access. Services keep setting `.status` as usual.
"""

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, UOWTransaction

from app.db.enums import StatusEntity
from app.db.models.billing import Invoice
from app.db.models.clinical import LabOrder
from app.db.models.workflow import Appointment, Referral, StatusEvent
from e2d_core.ids import uuid7

TRACKED: dict[type[Any], StatusEntity] = {
    Appointment: StatusEntity.APPOINTMENT,
    LabOrder: StatusEntity.LAB_ORDER,
    Referral: StatusEntity.REFERRAL,
    Invoice: StatusEntity.INVOICE,
}


def _initial_status(obj: Any) -> str:
    """The status a new row is inserted with (its column default when the service set none)."""
    if obj.status is not None:
        return str(obj.status)
    default = type(obj).__table__.c.status.server_default
    return str(default.arg)


@event.listens_for(Session, "before_flush")
def _record_status_changes(session: Session, flush: UOWTransaction, instances: Any) -> None:
    rows: list[StatusEvent] = []
    for obj in session.new:
        kind = TRACKED.get(type(obj))
        if kind is None:
            continue
        if obj.id is None:
            obj.id = uuid7()
        rows.append(
            StatusEvent(
                clinic_id=obj.clinic_id,
                entity_type=kind,
                entity_id=obj.id,
                status=_initial_status(obj),
            )
        )
    for obj in session.dirty:
        kind = TRACKED.get(type(obj))
        if kind is None:
            continue
        history = inspect(obj).attrs.status.history
        if history.added and list(history.added) != list(history.deleted):
            rows.append(
                StatusEvent(
                    clinic_id=obj.clinic_id,
                    entity_type=kind,
                    entity_id=obj.id,
                    status=str(history.added[0]),
                )
            )
    session.add_all(rows)

"""Read access to the workflow graph G_t (SPEC 5.1).

Two backends implement the same protocol: `SqlRepository` reads the product database and
`MemoryRepository` reads polars frames with the same table shapes (used by the simulator and the
experiments). Both must pass the same test suite.
"""

import uuid
from datetime import datetime, timedelta
from typing import Protocol

from e2d_core.explain.model import EvidenceBundle, EvidenceQuery

# How far around the access time evidence is fetched. Templates decide what actually counts.
APPOINTMENT_LOOKBACK = timedelta(days=90)
APPOINTMENT_LOOKAHEAD = timedelta(days=90)
ENCOUNTER_LOOKBACK = timedelta(days=120)
INVOICE_LOOKBACK = timedelta(days=60)
LAB_LOOKBACK = timedelta(days=60)
WINDOW_MARGIN = timedelta(days=1)
ASSIGNMENT_GRACE = timedelta(days=30)
PROGRESS_LOOKBACK = timedelta(days=7)
CREATION_WINDOW = timedelta(days=7)
# Records whose status history is kept (status_events.entity_type).
STATUS_ENTITIES = ("appointment", "lab_order", "referral", "invoice")


class EvidenceRepository(Protocol):
    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle: ...

    async def status_at(
        self, clinic_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID, at: datetime
    ) -> str | None:
        """The status the record had at `at` (the last one taken at or before it), or None if
        it had none yet."""
        ...


def median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


__all__ = [
    "APPOINTMENT_LOOKAHEAD",
    "APPOINTMENT_LOOKBACK",
    "ASSIGNMENT_GRACE",
    "CREATION_WINDOW",
    "ENCOUNTER_LOOKBACK",
    "INVOICE_LOOKBACK",
    "LAB_LOOKBACK",
    "PROGRESS_LOOKBACK",
    "STATUS_ENTITIES",
    "WINDOW_MARGIN",
    "EvidenceRepository",
    "median",
]

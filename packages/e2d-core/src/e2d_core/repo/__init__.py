"""Read access to the workflow graph G_t (SPEC 5.1).

Two backends implement the same protocol: `SqlRepository` reads the product database and
`MemoryRepository` reads polars frames with the same table shapes (used by the simulator and the
experiments). Both must pass the same test suite.
"""

from datetime import timedelta
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
BOOKING_NORM_WINDOW = timedelta(days=30)
# Fewer recent bookings than this and the clinic's booking norm is treated as unknown.
BOOKING_NORM_MIN = 20


class EvidenceRepository(Protocol):
    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle: ...


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
    "BOOKING_NORM_MIN",
    "BOOKING_NORM_WINDOW",
    "CREATION_WINDOW",
    "ENCOUNTER_LOOKBACK",
    "INVOICE_LOOKBACK",
    "LAB_LOOKBACK",
    "PROGRESS_LOOKBACK",
    "WINDOW_MARGIN",
    "EvidenceRepository",
    "median",
]

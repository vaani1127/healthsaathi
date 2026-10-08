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
INVOICE_LOOKBACK = timedelta(days=60)
SHIFT_MARGIN = timedelta(days=1)


class EvidenceRepository(Protocol):
    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle: ...


__all__ = [
    "APPOINTMENT_LOOKAHEAD",
    "APPOINTMENT_LOOKBACK",
    "INVOICE_LOOKBACK",
    "SHIFT_MARGIN",
    "EvidenceRepository",
]

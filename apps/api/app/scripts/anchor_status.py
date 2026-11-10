"""Per clinic: minutes since the last anchor and anchors in the last 24 hours (read-only).

    python -m app.scripts.anchor_status        # one JSON object, used by `make prod-status`

Reads as the anchor job role inside a read-only transaction. Counts and times only: no audit
payloads, no patient data.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from app.anchor.job import JOB_ROLE
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.models import AnchorReceipt, Clinic

DAY = timedelta(hours=24)


def summary(now: datetime, rows: list[Any]) -> dict[str, Any]:
    clinics: dict[str, dict[str, Any]] = {}
    for clinic_id, name, backend, last, recent in rows:
        entry = clinics.setdefault(
            str(clinic_id),
            {"name": name, "minutes_since_last_anchor": None, "anchors_24h": 0, "backends": {}},
        )
        if backend is None:
            continue
        minutes = round((now - last).total_seconds() / 60, 1)
        entry["backends"][backend] = {"minutes_since_last": minutes, "anchors_24h": recent}
        entry["anchors_24h"] += recent
        best = entry["minutes_since_last_anchor"]
        entry["minutes_since_last_anchor"] = minutes if best is None else min(best, minutes)
    return {"at": now.isoformat(), "clinics": clinics}


async def collect() -> dict[str, Any]:
    async with tenant_session(TenantContext(role="system", db_role=JOB_ROLE)) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        now = await db.scalar(text("SELECT clock_timestamp()"))
        rows = (
            await db.execute(
                select(
                    Clinic.id,
                    Clinic.name,
                    AnchorReceipt.backend,
                    func.max(AnchorReceipt.submitted_at),
                    func.count().filter(AnchorReceipt.submitted_at >= now - DAY),
                )
                .outerjoin(AnchorReceipt, AnchorReceipt.clinic_id == Clinic.id)
                .group_by(Clinic.id, Clinic.name, AnchorReceipt.backend)
                .order_by(Clinic.name)
            )
        ).all()
    return summary(now, [(c, n, b.value if b else None, last, k) for c, n, b, last, k in rows])


async def main() -> int:
    try:
        print(json.dumps(await collect()))
    finally:
        await get_engine().dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

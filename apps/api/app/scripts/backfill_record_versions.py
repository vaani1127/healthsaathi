"""Backfill record versions for clinical rows written before record versions existed (ADR 0011).

For every committable row with no record version yet (signed notes and prescriptions, every
vital, allergy, condition, lab order, lab result and document), it writes version 1 and its
`record_version` audit leaf, both marked "backfilled": true. Note and prescription version chains
are done oldest version first, so each later version links to its parent's version.

A backfilled version only shows that the record has not changed since the backfill ran, not
before. The integrity endpoint and the admin audit page say so.

Safe to run again: rows that already have a version are skipped, so a second run adds nothing.
It works in batches of BATCH rows per transaction, each under the clinic's audit-chain lock, so
it can run while the app is up.

Dry run by default (counts only). Local development only until the RUNBOOK steps are followed:

    make backfill-record-versions                    # dry run, every clinic
    make backfill-record-versions ARGS="--apply"     # write
    make backfill-record-versions ARGS="--apply --clinic <clinic uuid>"
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import ANONYMOUS, TenantContext, get_engine, tenant_session
from app.db.enums import RecordType
from app.db.models import Clinic, RecordVersion
from app.ledger.records import CHAINED, MODELS, Backfill, write_backfilled
from app.ledger.writer import lock_clinic

BATCH = 500

# Chained types last; within them, lower versions first (see pending()).
ORDER = (
    RecordType.VITAL,
    RecordType.ALLERGY,
    RecordType.CONDITION,
    RecordType.LAB_ORDER,
    RecordType.DOCUMENT,
    RecordType.LAB_RESULT,
    RecordType.NOTE,
    RecordType.PRESCRIPTION,
)


def _ctx(clinic_id: uuid.UUID) -> TenantContext:
    return TenantContext(clinic_id=clinic_id, role="system")


def _no_version(record_type: RecordType, model: Any) -> Any:
    return ~exists().where(
        RecordVersion.clinic_id == model.clinic_id,
        RecordVersion.record_type == record_type,
        RecordVersion.record_id == model.id,
    )


async def pending(db: AsyncSession, clinic_id: uuid.UUID, limit: int) -> list[Backfill]:
    """Up to `limit` committable rows of the clinic that have no record version, in ORDER.
    Note and prescription rows come lowest version first, so a parent is always backfilled in
    an earlier batch or earlier in the same batch than its child."""
    items: list[Backfill] = []
    new_ids: dict[tuple[RecordType, uuid.UUID], uuid.UUID] = {}
    for record_type in ORDER:
        if len(items) >= limit:
            break
        model = MODELS[record_type]
        query = select(model).where(model.clinic_id == clinic_id, _no_version(record_type, model))
        if record_type in CHAINED:
            query = query.where(model.signed_at.is_not(None)).order_by(
                model.version, model.created_at, model.id
            )
        else:
            query = query.order_by(model.id)
        rows = list((await db.scalars(query.limit(limit - len(items)))).all())
        parents: dict[uuid.UUID, uuid.UUID] = {}
        if record_type in CHAINED:
            parent_ids = [r.parent_id for r in rows if r.parent_id is not None]
            if parent_ids:
                found = await db.execute(
                    select(RecordVersion.record_id, RecordVersion.id)
                    .where(
                        RecordVersion.clinic_id == clinic_id,
                        RecordVersion.record_type == record_type,
                        RecordVersion.record_id.in_(parent_ids),
                    )
                    .order_by(RecordVersion.version)
                )
                parents = {record_id: version_id for record_id, version_id in found.all()}
        for row in rows:
            prev = None
            if record_type in CHAINED and row.parent_id is not None:
                prev = new_ids.get((record_type, row.parent_id)) or parents.get(row.parent_id)
            item = Backfill(record_type, row, prev)
            new_ids[(record_type, row.id)] = item.version_id
            items.append(item)
    return items


async def count_pending(clinic_id: uuid.UUID) -> Counter[str]:
    counts: Counter[str] = Counter()
    async with tenant_session(_ctx(clinic_id)) as db:
        for record_type in ORDER:
            model = MODELS[record_type]
            query = select(model.id).where(
                model.clinic_id == clinic_id, _no_version(record_type, model)
            )
            if record_type in CHAINED:
                query = query.where(model.signed_at.is_not(None))
            counts[record_type.value] = len((await db.scalars(query)).all())
    return counts


async def backfill_clinic(clinic_id: uuid.UUID, batch: int = BATCH) -> Counter[str]:
    """Backfill one clinic. Returns how many versions were written, per record type."""
    written: Counter[str] = Counter()
    while True:
        async with tenant_session(_ctx(clinic_id)) as db:
            # Taken before reading, so no app write can add a version to these rows meanwhile.
            await lock_clinic(db, "audit-chain", clinic_id)
            items = await pending(db, clinic_id, batch)
            if not items:
                return written
            await write_backfilled(db, clinic_id, items)
        written.update(item.record_type.value for item in items)


async def clinic_ids(only: uuid.UUID | None) -> list[uuid.UUID]:
    if only is not None:
        return [only]
    async with tenant_session(ANONYMOUS) as db:
        return list((await db.scalars(select(Clinic.id).order_by(Clinic.id))).all())


def database_label() -> str:
    url = make_url(get_settings().database_url)
    return f"{url.host or 'local socket'}:{url.port or 5432}/{url.database}"


async def run(apply: bool, only: uuid.UUID | None) -> dict[str, Any]:
    report: dict[str, Any] = {"database": database_label(), "applied": apply, "clinics": {}}
    try:
        for clinic_id in await clinic_ids(only):
            if apply:
                counts = await backfill_clinic(clinic_id)
            else:
                counts = await count_pending(clinic_id)
            report["clinics"][str(clinic_id)] = dict(sorted(counts.items()))
    finally:
        await get_engine().dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--clinic", type=uuid.UUID, default=None, help="only this clinic")
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.apply, args.clinic))
    print(json.dumps(report, indent=2))
    if not args.apply:
        print("Dry run: nothing written. Add --apply to write.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

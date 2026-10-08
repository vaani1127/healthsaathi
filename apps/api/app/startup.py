"""Tasks run when an API or worker process starts. Safe to run many times."""

import asyncio
import logging

from sqlalchemy import select, text

from app.access.policy import get_policy
from app.core.db import ANONYMOUS, tenant_session
from app.db.models import PARTITIONED_TABLES, PolicyVersion

logger = logging.getLogger(__name__)

PARTITION_MONTHS_AHEAD = 6


async def register_policy_version() -> str:
    """Store the active policy file so every policy_version hash can be looked up later."""
    policy = get_policy()
    async with tenant_session(ANONYMOUS) as db:
        found = await db.scalar(
            select(PolicyVersion.id).where(PolicyVersion.sha256 == policy.sha256)
        )
        if found is None:
            db.add(
                PolicyVersion(version=policy.version, yaml=policy.yaml_text, sha256=policy.sha256)
            )
    return policy.sha256


async def ensure_partitions() -> int:
    created = 0
    async with tenant_session(ANONYMOUS) as db:
        for table in PARTITIONED_TABLES:
            created += int(
                await db.scalar(
                    text("SELECT create_month_partitions(:t, 0, :ahead)"),
                    {"t": table, "ahead": PARTITION_MONTHS_AHEAD},
                )
                or 0
            )
    return created


MAINTENANCE_INTERVAL_SECONDS = 12 * 3600


async def maintenance_loop() -> None:
    """Keep future partitions in place while the process runs. Both API workers may run this;
    create_month_partitions() is idempotent."""
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
        try:
            await ensure_partitions()
        except Exception:
            logger.exception("partition maintenance failed")


async def run_startup_tasks() -> None:
    try:
        sha = await register_policy_version()
        created = await ensure_partitions()
        logger.info("startup done", extra={"policy_version": sha, "partitions_created": created})
    except Exception:
        # The API still starts; /ready reports the database problem.
        logger.exception("startup tasks failed")

"""Collect the study data: timestamps from the database, then gas and block times from the chains.

Only leaf positions, timestamps, tree sizes and transaction references leave the database.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

EVM_BACKENDS = ("amoy", "besu")
TS = pl.Datetime("us", "UTC")

EVENTS_SQL = """
WITH e AS (
    SELECT clinic_id, row_number() OVER (PARTITION BY clinic_id ORDER BY seq) - 1 AS idx, at
    FROM audit_events
)
SELECT clinic_id::text AS clinic_id, idx, at FROM e WHERE at >= $1 ORDER BY clinic_id, idx
"""
CHECKPOINTS_SQL = """
SELECT id::text AS checkpoint_id, clinic_id::text AS clinic_id, tree_size, signed_at
FROM merkle_checkpoints ORDER BY clinic_id, tree_size
"""
RECEIPTS_SQL = """
SELECT checkpoint_id::text AS checkpoint_id, clinic_id::text AS clinic_id, backend::text AS backend,
       status::text AS status, tx_ref, block_ref, submitted_at, confirmed_at
FROM anchor_receipts ORDER BY submitted_at
"""


def plain_dsn(url: str) -> str:
    """asyncpg wants postgresql:// and sslmode; the app's settings use +asyncpg and ssl."""
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "ssl=require", "sslmode=require"
    )


def _frame(rows: list[Any], schema: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame([dict(r) for r in rows], schema=schema, orient="row")


async def snapshot(url: str, since: datetime, out: Path) -> dict[str, int]:
    import asyncpg

    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    conn = await asyncpg.connect(plain_dsn(url))
    try:
        async with conn.transaction(readonly=True):
            events = await conn.fetch(EVENTS_SQL, since)
            checkpoints = await conn.fetch(CHECKPOINTS_SQL)
            receipts = await conn.fetch(RECEIPTS_SQL)
    finally:
        await conn.close()
    frames = {
        "events": _frame(events, {"clinic_id": pl.String, "idx": pl.Int64, "at": TS}),
        "checkpoints": _frame(
            checkpoints,
            {
                "checkpoint_id": pl.String,
                "clinic_id": pl.String,
                "tree_size": pl.Int64,
                "signed_at": TS,
            },
        ),
        "receipts": _frame(
            receipts,
            {
                "checkpoint_id": pl.String,
                "clinic_id": pl.String,
                "backend": pl.String,
                "status": pl.String,
                "tx_ref": pl.String,
                "block_ref": pl.String,
                "submitted_at": TS,
                "confirmed_at": TS,
            },
        ),
    }
    for name, frame in frames.items():
        frame.write_parquet(out / f"{name}.parquet")
    (out / "snapshot.txt").write_text(
        f"taken_at={datetime.now(UTC).isoformat()}\nsince={since.isoformat()}\n", encoding="utf-8"
    )
    return {k: v.height for k, v in frames.items()}


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    resp = await client.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


async def evm_details(client: httpx.AsyncClient, url: str, tx: str) -> dict[str, Any]:
    receipt = await _rpc(client, url, "eth_getTransactionReceipt", [tx])
    block = await _rpc(client, url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
    return {
        "gas_used": int(receipt["gasUsed"], 16),
        "gas_price_wei": int(receipt["effectiveGasPrice"], 16),
        "final_at": datetime.fromtimestamp(int(block["timestamp"], 16), UTC),
    }


async def bitcoin_time(client: httpx.AsyncClient, api: str, height: int) -> datetime:
    block_hash = (await client.get(f"{api}/block-height/{height}")).raise_for_status().text.strip()
    block = (await client.get(f"{api}/block/{block_hash}")).raise_for_status().json()
    return datetime.fromtimestamp(int(block["timestamp"]), UTC)


async def enrich(folder: Path, rpc: dict[str, str], bitcoin_api: str) -> int:
    """Add final_at, gas_used and gas_price_wei to receipts.parquet. Rows already done are kept."""
    path = folder / "receipts.parquet"
    receipts = pl.read_parquet(path)
    old = folder / "enriched.parquet"
    done = pl.read_parquet(old) if old.exists() else None
    known = {} if done is None else {(r["checkpoint_id"], r["backend"]): r for r in done.to_dicts()}
    rows = []
    async with httpx.AsyncClient(timeout=30) as client:
        for row in receipts.to_dicts():
            extra: dict[str, Any] = {"final_at": None, "gas_used": None, "gas_price_wei": None}
            cached = known.get((row["checkpoint_id"], row["backend"]))
            if cached and cached["final_at"] is not None:
                extra = {k: cached[k] for k in extra}
            elif row["status"] == "confirmed" and row["backend"] in EVM_BACKENDS and row["tx_ref"]:
                extra = await evm_details(client, rpc[row["backend"]], row["tx_ref"])
            elif row["status"] == "confirmed" and row["backend"] == "ots" and row["block_ref"]:
                extra["final_at"] = await bitcoin_time(client, bitcoin_api, int(row["block_ref"]))
            rows.append({**row, **extra})
    schema = receipts.schema
    schema.update({"final_at": TS, "gas_used": pl.Int64(), "gas_price_wei": pl.Int64()})
    frame = pl.DataFrame(rows, schema=schema, orient="row")
    frame.write_parquet(old)
    return int(frame["final_at"].is_not_null().sum())


def run_snapshot(url: str, since: datetime, out: Path) -> dict[str, int]:
    return asyncio.run(snapshot(url, since, out))


def run_enrich(folder: Path, rpc: dict[str, str], bitcoin_api: str) -> int:
    return asyncio.run(enrich(folder, rpc, bitcoin_api))

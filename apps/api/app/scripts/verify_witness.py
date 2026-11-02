"""Check a clinic's audit log against the signed tree heads on its witness (an auditor's tool).

    uv run python -m app.scripts.verify_witness <clinic-uuid> --repo owner/healthsaathi-witness
    uv run python -m app.scripts.verify_witness <clinic-uuid> --dir /path/to/witness-folder

Each published head must carry a valid signature by the clinic key, and the log in the database
must still have the same Merkle root at the head's size. A database owner who rewrote audit rows
and deleted the stored checkpoints is caught here, because the published heads cannot be changed.
Reads the database as the anchor job role; prints one line per head and exits 1 on any mismatch.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.anchor.job import JOB_ROLE
from app.core.db import TenantContext, get_engine, tenant_session
from app.ledger.checkpoint import LedgerIntegrityError, clinic_signing_key, load_tree
from e2d_core.ledger import public_key_bytes
from e2d_core.ledger.sth import TreeHead
from e2d_core.ledger.witness import HeadCheck, PublishedHead, check_log

GITHUB_API = "https://api.github.com"


def parse(document: dict[str, Any]) -> PublishedHead:
    return PublishedHead(
        TreeHead.from_json(document["sth"]),
        bytes.fromhex(document["signature"]),
        bytes.fromhex(document["public_key"]),
    )


def heads_from_folder(folder: Path, clinic_id: uuid.UUID) -> list[PublishedHead]:
    files = sorted((folder / "witness" / str(clinic_id)).glob("*.json"))
    return [parse(json.loads(f.read_text(encoding="utf-8"))) for f in files]


def heads_from_github(repo: str, clinic_id: uuid.UUID, branch: str = "main") -> list[PublishedHead]:
    """The public contents API; no token needed for a public witness repository."""
    with httpx.Client(timeout=30) as client:
        listing = client.get(
            f"{GITHUB_API}/repos/{repo}/contents/witness/{clinic_id}", params={"ref": branch}
        )
        if listing.status_code == 404:
            return []
        listing.raise_for_status()
        heads = []
        for item in listing.json():
            if item["name"].endswith(".json"):
                raw = client.get(item["download_url"])
                raw.raise_for_status()
                heads.append(parse(raw.json()))
    return heads


async def verify(clinic_id: uuid.UUID, heads: list[PublishedHead]) -> list[HeadCheck]:
    async with tenant_session(
        TenantContext(clinic_id=clinic_id, role="system", db_role=JOB_ROLE)
    ) as db:
        tree, _ = await load_tree(db, clinic_id)
    return check_log(tree, heads, public_key_bytes(clinic_signing_key(clinic_id)))


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clinic_id", type=uuid.UUID)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="public GitHub witness repository, owner/name")
    source.add_argument("--dir", type=Path, help="local witness folder")
    args = parser.parse_args(argv)
    heads = (
        heads_from_github(args.repo, args.clinic_id)
        if args.repo
        else heads_from_folder(args.dir, args.clinic_id)
    )
    if not heads:
        print("no published heads for this clinic")
        return 1
    try:
        results = await verify(args.clinic_id, heads)
    except LedgerIntegrityError as exc:
        print(f"FAIL: the log itself is broken: {exc}")
        return 1
    finally:
        await get_engine().dispose()
    for r in results:
        print(f"size {r.tree_size}: {'PASS' if r.ok else 'FAIL: ' + (r.reason or '')}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

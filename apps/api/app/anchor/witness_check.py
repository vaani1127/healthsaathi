"""Check a clinic's audit log against the signed tree heads on its witness (SPEC 6).

The witness holds one file per published head, witness/<clinic_id>/<tree_size>.json, in a local
git folder (WITNESS_DIR) or the public GitHub repository (WITNESS_REPO). A database owner who
rewrote audit rows and deleted the stored checkpoints is caught here, because the published heads
cannot be changed. The check itself is `e2d_core.ledger.witness.check_log`.

The auditor's script (`app.scripts.verify_witness`) checks every published head. The admin audit
page checks the newest one: its Merkle root covers every event up to its size, so any rewritten
event before it changes that root. GitHub reads are cached for a few minutes per clinic, because
the page refreshes every 30 seconds and published heads never change.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.ledger.checkpoint import clinic_signing_key
from e2d_core.ledger import public_key_bytes
from e2d_core.ledger.merkle import MerkleTree
from e2d_core.ledger.sth import TreeHead
from e2d_core.ledger.witness import PublishedHead, check_log

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
CACHE_SECONDS = 300
_cache: dict[tuple[str, uuid.UUID], tuple[float, PublishedHead | None]] = {}


@dataclass(frozen=True)
class WitnessResult:
    """ok is None when no witness is configured, nothing is published yet, or it is unreachable."""

    ok: bool | None
    tree_size: int | None = None
    reason: str | None = None


def parse(document: dict[str, Any]) -> PublishedHead:
    return PublishedHead(
        TreeHead.from_json(document["sth"]),
        bytes.fromhex(document["signature"]),
        bytes.fromhex(document["public_key"]),
    )


def _size(name: str) -> int | None:
    stem, _, ext = name.rpartition(".")
    return int(stem) if ext == "json" and stem.isdigit() else None


def heads_from_folder(folder: Path, clinic_id: uuid.UUID) -> list[PublishedHead]:
    files = sorted((folder / "witness" / str(clinic_id)).glob("*.json"))
    return [parse(json.loads(f.read_text(encoding="utf-8"))) for f in files]


def heads_from_github(repo: str, clinic_id: uuid.UUID, branch: str = "main") -> list[PublishedHead]:
    """Every head, through the public contents API; no token needed for a public repository."""
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


def newest_in_folder(folder: Path, clinic_id: uuid.UUID) -> PublishedHead | None:
    files = [
        (size, f)
        for f in (folder / "witness" / str(clinic_id)).glob("*.json")
        if (size := _size(f.name)) is not None
    ]
    if not files:
        return None
    _, newest = max(files)
    return parse(json.loads(newest.read_text(encoding="utf-8")))


async def newest_on_github(
    repo: str,
    clinic_id: uuid.UUID,
    branch: str,
    token: str | None,
    client: httpx.AsyncClient | None = None,
) -> PublishedHead | None:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    http = client or httpx.AsyncClient(timeout=10)
    try:
        listing = await http.get(
            f"{GITHUB_API}/repos/{repo}/contents/witness/{clinic_id}",
            params={"ref": branch},
            headers=headers,
        )
        if listing.status_code == 404:
            return None
        listing.raise_for_status()
        files = [
            (size, item["download_url"])
            for item in listing.json()
            if (size := _size(item["name"])) is not None
        ]
        if not files:
            return None
        _, url = max(files)
        raw = await http.get(url)
        raw.raise_for_status()
        return parse(raw.json())
    finally:
        if own:
            await http.aclose()


async def newest_head(
    clinic_id: uuid.UUID, folder: Path | None = None, client: httpx.AsyncClient | None = None
) -> tuple[bool, PublishedHead | None]:
    """(configured, newest head). `folder` overrides the settings (the tamper study uses it)."""
    s = get_settings()
    if folder is None and s.witness_dir:
        folder = Path(s.witness_dir)
    if folder is not None:
        return True, newest_in_folder(folder, clinic_id)
    if not s.witness_repo:
        return False, None
    key = (s.witness_repo, clinic_id)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return True, cached[1]
    token = s.witness_github_token.get_secret_value() if s.witness_github_token else None
    head = await newest_on_github(s.witness_repo, clinic_id, s.witness_branch, token, client)
    _cache[key] = (time.monotonic() + CACHE_SECONDS, head)
    return True, head


async def check_newest(
    tree: MerkleTree,
    clinic_id: uuid.UUID,
    folder: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> WitnessResult:
    """The log against the newest published head. A witness that cannot be read is not a fail."""
    try:
        configured, head = await newest_head(clinic_id, folder, client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("witness check skipped: %s", exc)
        return WitnessResult(None, reason="the witness could not be read")
    if not configured or head is None:
        return WitnessResult(None)
    [result] = check_log(tree, [head], public_key_bytes(clinic_signing_key(clinic_id)))
    return WitnessResult(result.ok, result.tree_size, result.reason)

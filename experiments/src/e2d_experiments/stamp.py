"""Timestamp the pre-registration commit on Bitcoin through public OpenTimestamps calendars.

    uv run python -m e2d_experiments.stamp            # after committing PREREGISTRATION.md
    uv run python -m e2d_experiments.stamp --upgrade  # a few hours later, to complete the proof

Writes the commit hash to experiments/PREREGISTRATION.commit and a standard detached proof of
that file to experiments/PREREGISTRATION.ots. Anyone can check it with the official client:
`ots verify experiments/PREREGISTRATION.ots`.
"""

import argparse
import hashlib
import io
import subprocess
import sys
from pathlib import Path

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, StreamSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
)
COMMIT_FILE = Path("experiments/PREREGISTRATION.commit")
PROOF_FILE = Path("experiments/PREREGISTRATION.ots")


def _write(stamp: DetachedTimestampFile) -> None:
    buffer = io.BytesIO()
    stamp.serialize(StreamSerializationContext(buffer))
    PROOF_FILE.write_bytes(buffer.getvalue())


def _confirmed(timestamp: Timestamp) -> bool:
    return any(
        isinstance(a, BitcoinBlockHeaderAttestation) for _, a in timestamp.all_attestations()
    )


def stamp_commit(commit: str) -> int:
    COMMIT_FILE.write_text(commit + "\n", encoding="utf-8")
    digest = hashlib.sha256(COMMIT_FILE.read_bytes()).digest()
    stamp = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    accepted = 0
    for url in CALENDARS:
        try:
            stamp.timestamp.merge(RemoteCalendar(url).submit(digest, timeout=15))
            accepted += 1
        except Exception as exc:  # an unreachable calendar is skipped
            print(f"{url}: {type(exc).__name__}")
    if accepted == 0:
        print("no calendar accepted the stamp; try again later")
        return 1
    _write(stamp)
    print(f"stamped {commit} on {accepted} calendars; run --upgrade in a few hours")
    return 0


def upgrade() -> int:
    stamp = DetachedTimestampFile.deserialize(BytesDeserializationContext(PROOF_FILE.read_bytes()))
    for msg, attestation in list(stamp.timestamp.all_attestations()):
        if isinstance(attestation, PendingAttestation):
            try:
                found = RemoteCalendar(attestation.uri).get_timestamp(msg, timeout=15)
            except Exception as exc:  # not ready yet
                print(f"{attestation.uri}: {type(exc).__name__}")
                found = None
            for node in [stamp.timestamp, *_nodes(stamp.timestamp)]:
                if found is not None and node.msg == msg:
                    node.merge(found)
    _write(stamp)
    done = _confirmed(stamp.timestamp)
    print("confirmed in a Bitcoin block" if done else "still pending; try again later")
    return 0


def _nodes(timestamp: Timestamp) -> list[Timestamp]:
    out = []
    for child in timestamp.ops.values():
        out.append(child)
        out.extend(_nodes(child))
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="OpenTimestamps proof of the registration commit.")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--commit", help="commit to stamp (default: the last commit that touched "
                        "experiments/PREREGISTRATION.md)")  # fmt: skip
    args = parser.parse_args(argv)
    if args.upgrade:
        return upgrade()
    commit = (
        args.commit
        or subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", "experiments/PREREGISTRATION.md"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    if not commit:
        print("commit experiments/PREREGISTRATION.md first")
        return 1
    return stamp_commit(commit)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

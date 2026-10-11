"""The stamped commit file is written with LF endings, as git stores it, and the proof is of
exactly those bytes."""

import hashlib
from pathlib import Path

import pytest
from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.serialize import BytesDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from e2d_experiments import stamp

COMMIT = "14f5feb4fd8e93829581cd532ca34b1d55679fe5"


class FakeCalendar:
    def __init__(self, url: str) -> None:
        self.url = url

    def submit(self, digest: bytes, timeout: int) -> Timestamp:
        timestamp = Timestamp(digest)
        timestamp.attestations.add(PendingAttestation(self.url))
        return timestamp


def test_commit_file_matches_its_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stamp, "COMMIT_FILE", tmp_path / "PREREGISTRATION.commit")
    monkeypatch.setattr(stamp, "PROOF_FILE", tmp_path / "PREREGISTRATION.ots")
    monkeypatch.setattr(stamp, "RemoteCalendar", FakeCalendar)
    assert stamp.stamp_commit(COMMIT) == 0
    written = stamp.COMMIT_FILE.read_bytes()
    assert written == f"{COMMIT}\n".encode("ascii"), "LF only, as git stores it"
    proof = DetachedTimestampFile.deserialize(
        BytesDeserializationContext(stamp.PROOF_FILE.read_bytes())
    )
    assert proof.file_digest == hashlib.sha256(written).digest()

"""OpenTimestamps: stamp the SHA-256 of each signed tree head on public calendars, then upgrade the
pending proof once the calendars have committed it to Bitcoin.
"""

import io
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, StreamSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

logger = logging.getLogger(__name__)


class Calendar(Protocol):
    url: str

    def submit(self, digest: bytes, timeout: float | None = None) -> Timestamp: ...

    def get_timestamp(self, commitment: bytes, timeout: float | None = None) -> Timestamp: ...


@dataclass(frozen=True)
class OtsProof:
    data: bytes  # serialized .ots file
    bitcoin_height: int | None  # set once a Bitcoin attestation is present


def _serialize(stamp: DetachedTimestampFile) -> bytes:
    buf = io.BytesIO()
    stamp.serialize(StreamSerializationContext(buf))
    return buf.getvalue()


def _bitcoin_height(timestamp: Timestamp) -> int | None:
    heights = [
        a.height
        for _, a in timestamp.all_attestations()
        if isinstance(a, BitcoinBlockHeaderAttestation)
    ]
    return min(heights) if heights else None


def _nodes(timestamp: Timestamp) -> Iterator[Timestamp]:
    """Every timestamp in the tree, so pending attestations can be upgraded where they sit."""
    yield timestamp
    for child in timestamp.ops.values():
        yield from _nodes(child)


class OtsStamper:
    def __init__(self, calendars: Sequence[Calendar] | Sequence[str], timeout: float = 10) -> None:
        self.calendars: list[Calendar] = [
            RemoteCalendar(c) if isinstance(c, str) else c for c in calendars
        ]
        self.timeout = timeout

    def stamp(self, digest: bytes) -> OtsProof:
        """Submit to every calendar that answers; at least one must succeed."""
        stamp = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
        merged = 0
        for calendar in self.calendars:
            try:
                stamp.timestamp.merge(calendar.submit(digest, timeout=self.timeout))
                merged += 1
            except Exception:  # an unreachable calendar is skipped
                logger.warning("calendar did not accept the digest", extra={"url": calendar.url})
        if merged == 0:
            raise RuntimeError("no OpenTimestamps calendar accepted the digest")
        return OtsProof(_serialize(stamp), _bitcoin_height(stamp.timestamp))

    def upgrade(self, proof: bytes) -> OtsProof:
        """Ask the calendars for the completed path to Bitcoin for each pending attestation."""
        stamp = DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
        by_url = {c.url.rstrip("/"): c for c in self.calendars}
        for node in list(_nodes(stamp.timestamp)):
            for attestation in list(node.attestations):
                if not isinstance(attestation, PendingAttestation):
                    continue
                calendar = by_url.get(attestation.uri.rstrip("/"))
                if calendar is None:
                    continue
                try:
                    node.merge(calendar.get_timestamp(node.msg, timeout=self.timeout))
                except Exception:  # not ready yet
                    logger.info("calendar proof not ready", extra={"url": calendar.url})
        return OtsProof(_serialize(stamp), _bitcoin_height(stamp.timestamp))

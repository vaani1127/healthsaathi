"""OpenTimestamps: stamp the SHA-256 of each signed tree head on public calendars, then upgrade the
pending proof once the calendars have committed it to Bitcoin.
"""

import io
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, StreamSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

logger = logging.getLogger(__name__)

# The pool servers that stamps are submitted to answer with pending attestations that name the
# calendar that will commit to Bitcoin (for example alice.btc.calendar.opentimestamps.org). Like the
# official client, ask those calendars too, but only on these well-known domains.
TRUSTED_CALENDAR_DOMAINS = (
    ".calendar.opentimestamps.org",
    ".calendar.eternitywall.com",
    ".calendar.catallaxy.com",
)


def trusted_calendar(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and host.endswith(TRUSTED_CALENDAR_DOMAINS)


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
    def __init__(
        self,
        calendars: Sequence[Calendar] | Sequence[str],
        timeout: float = 10,
        remote: Callable[[str], Calendar] = RemoteCalendar,
    ) -> None:
        self.calendars: list[Calendar] = [remote(c) if isinstance(c, str) else c for c in calendars]
        self.timeout = timeout
        self.remote = remote

    def _calendar_for(self, uri: str) -> Calendar | None:
        for calendar in self.calendars:
            if calendar.url.rstrip("/") == uri.rstrip("/"):
                return calendar
        return self.remote(uri) if trusted_calendar(uri) else None

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
        for node in list(_nodes(stamp.timestamp)):
            for attestation in list(node.attestations):
                if not isinstance(attestation, PendingAttestation):
                    continue
                calendar = self._calendar_for(attestation.uri)
                if calendar is None:
                    logger.warning("untrusted calendar in proof", extra={"url": attestation.uri})
                    continue
                try:
                    node.merge(calendar.get_timestamp(node.msg, timeout=self.timeout))
                except Exception:  # not ready yet
                    logger.info("calendar proof not ready", extra={"url": calendar.url})
        return OtsProof(_serialize(stamp), _bitcoin_height(stamp.timestamp))

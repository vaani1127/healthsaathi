from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.timestamp import Timestamp

from app.anchor.ots import Calendar, OtsStamper, trusted_calendar

POOL = "https://a.pool.opentimestamps.org"
ALICE = "https://alice.btc.calendar.opentimestamps.org"


class FakeCalendar:
    """A pool hands back an attestation that names another calendar, as the real pools do."""

    def __init__(self, url: str, points_to: str | None = None, height: int | None = None) -> None:
        self.url = url
        self.points_to = points_to or url
        self.height = height
        self.asked = 0

    def submit(self, digest: bytes, timeout: float | None = None) -> Timestamp:
        stamp = Timestamp(digest)
        stamp.attestations.add(PendingAttestation(self.points_to))
        return stamp

    def get_timestamp(self, commitment: bytes, timeout: float | None = None) -> Timestamp:
        self.asked += 1
        if self.height is None:
            raise RuntimeError("not ready")
        stamp = Timestamp(commitment)
        stamp.attestations.add(BitcoinBlockHeaderAttestation(self.height))
        return stamp


def test_upgrade_asks_the_calendar_named_in_the_attestation() -> None:
    alice = FakeCalendar(ALICE, height=921_000)
    made: list[str] = []

    def remote(url: str) -> Calendar:
        made.append(url)
        return alice

    stamper = OtsStamper([FakeCalendar(POOL, points_to=ALICE)], remote=remote)
    proof = stamper.stamp(b"\x01" * 32)
    assert proof.bitcoin_height is None
    upgraded = stamper.upgrade(proof.data)
    assert made == [ALICE]
    assert alice.asked == 1
    assert upgraded.bitcoin_height == 921_000


def test_upgrade_ignores_calendars_outside_the_trusted_domains() -> None:
    made: list[str] = []

    def remote(url: str) -> Calendar:
        made.append(url)
        return FakeCalendar(url, height=1)

    stamper = OtsStamper([FakeCalendar(POOL, points_to="https://evil.example/cal")], remote=remote)
    proof = stamper.stamp(b"\x02" * 32)
    assert stamper.upgrade(proof.data).bitcoin_height is None
    assert made == []


def test_trusted_calendar_domains() -> None:
    assert trusted_calendar(ALICE)
    assert trusted_calendar("https://finney.calendar.eternitywall.com")
    assert not trusted_calendar("http://alice.btc.calendar.opentimestamps.org")
    assert not trusted_calendar("https://calendar.opentimestamps.org.evil.example")
    assert not trusted_calendar("https://evil.example")

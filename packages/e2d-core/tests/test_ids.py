import time

from e2d_core.ids import uuid7, uuid7_timestamp_ms


def test_version_and_variant() -> None:
    u = uuid7()
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"


def test_timestamp_is_close_to_now() -> None:
    now = int(time.time() * 1000)
    assert abs(uuid7_timestamp_ms(uuid7()) - now) < 2000


def test_monotonic_within_process() -> None:
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_explicit_timestamp() -> None:
    u = uuid7(timestamp_ms=1_700_000_000_000)
    assert uuid7_timestamp_ms(u) == 1_700_000_000_000
    assert u.version == 7

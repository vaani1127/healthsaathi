"""UUID version 7 (RFC 9562): 48-bit unix millisecond timestamp followed by random bits.

Python 3.12 has no uuid7, so it is implemented here and shared by the API and the simulator.
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Return a new UUIDv7. Ids made in the same millisecond keep increasing (12-bit counter)."""
    global _last_ms, _counter
    with _lock:
        ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        if ms <= _last_ms and timestamp_ms is None:
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:
                ms += 1
                _counter = 0
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        if timestamp_ms is None:
            _last_ms = ms
        counter = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Return the millisecond timestamp stored in a UUIDv7."""
    return value.int >> 80

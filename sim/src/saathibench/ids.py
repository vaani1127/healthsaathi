"""Deterministic UUIDv7 ids: the timestamp comes from simulated time and the random bits from the
clinic's seeded generator, so the same seed always gives the same ids."""

import random
import uuid
from datetime import datetime


class Ids:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def new(self, at: datetime) -> str:
        ms = int(at.timestamp() * 1000) & ((1 << 48) - 1)
        rand_a = self.rng.getrandbits(12)
        rand_b = self.rng.getrandbits(62)
        value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
        return str(uuid.UUID(int=value))

    def hex(self, nbytes: int = 16) -> str:
        return self.rng.getrandbits(nbytes * 8).to_bytes(nbytes, "big").hex()

"""Reading a run's labels. Only evaluation code (the audit, splits and experiment scoring) may
import this module; feature and detector code must not (enforced by import-linter)."""

from pathlib import Path

import polars as pl

from saathibench.schema import LABEL_TABLES


def read_labels(run: Path, table: str = "access_labels") -> pl.DataFrame:
    schema = LABEL_TABLES[table]
    files = sorted((run / "labels" / table).glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=schema)
    return pl.concat([pl.read_parquet(f) for f in files])


def attack_labels(run: Path) -> pl.DataFrame:
    """access_event_id, is_attack, attack_type, campaign_id, mimicry for every access event."""
    return read_labels(run, "access_labels")


def campaigns(run: Path) -> pl.DataFrame:
    return read_labels(run, "campaigns")

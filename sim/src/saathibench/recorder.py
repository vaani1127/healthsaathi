"""Collects rows per table and writes them as Parquet parts.

Tables whose rows change after they are created (an appointment that is later completed) stay in
memory until the clinic is finished; they are small. Append-only tables such as access_events are
written to `<out>/tables/<table>/<clinic>-<part>.parquet` every `flush_rows` rows, so a long run
never keeps a whole clinic's accesses in memory. Labels go to `<out>/labels/`, never `tables/`.
"""

from pathlib import Path
from typing import Any

import polars as pl

from saathibench.schema import LABEL_TABLES, TABLES

# Rows of these tables are updated in place after they are added.
MUTABLE = frozenset(
    {"appointments", "queue_tokens", "lab_orders", "lab_results", "invoices", "referrals"}
)

Row = dict[str, Any]


class Recorder:
    def __init__(self, out: Path, clinic_key: str, flush_rows: int = 200_000) -> None:
        self.out = out
        self.clinic_key = clinic_key
        self.flush_rows = flush_rows
        self.rows: dict[str, list[Row]] = {t: [] for t in (*TABLES, *LABEL_TABLES)}
        self.parts: dict[str, int] = {}
        self.counts: dict[str, int] = {}

    def add(self, table: str, row: Row) -> Row:
        """Buffer a row and return it; rows of MUTABLE tables may be changed until close()."""
        rows = self.rows[table]
        rows.append(row)
        if table not in MUTABLE and len(rows) >= self.flush_rows:
            self._flush(table)
        return row

    def _flush(self, table: str) -> None:
        rows = self.rows[table]
        if not rows:
            return
        is_label = table in LABEL_TABLES
        schema = LABEL_TABLES[table] if is_label else TABLES[table]
        folder = self.out / ("labels" if is_label else "tables") / table
        folder.mkdir(parents=True, exist_ok=True)
        part = self.parts.get(table, 0)
        columns = {c: [r.get(c) for r in rows] for c in schema}
        frame = pl.DataFrame(columns, schema=schema)
        frame.write_parquet(folder / f"{self.clinic_key}-{part:03d}.parquet", compression="zstd")
        self.parts[table] = part + 1
        self.counts[table] = self.counts.get(table, 0) + len(rows)
        self.rows[table] = []

    def close(self) -> dict[str, int]:
        for table in self.rows:
            self._flush(table)
        return dict(self.counts)

"""Statement-time defaults for the timestamps the explanation engine reads.

now() is the time the transaction began, so a row created after an access in the same request
was dated before the access. clock_timestamp() is the time of the insert. Rows written before
this revision keep their transaction-time stamps. Audit and anchoring timestamps are unchanged.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) whose default becomes clock_timestamp(). Kept in step with
# tests/db/test_statement_time.py.
COLUMNS: tuple[tuple[str, str], ...] = (
    *(
        (table, "created_at")
        for table in (
            "patients",
            "appointments",
            "queue_tokens",
            "encounters",
            "referrals",
            "care_team_assignments",
            "invoices",
            "lab_orders",
            "shifts",
            "schedules",
            "clinical_notes",
            "prescriptions",
            "vitals",
        )
    ),
    ("status_events", "at"),
    ("vitals", "recorded_at"),
    ("lab_results", "resulted_at"),
    ("break_glass_events", "at"),
)


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT clock_timestamp()")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE migrator")
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT now()")
    op.execute("RESET ROLE")

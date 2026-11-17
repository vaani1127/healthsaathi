"""Rate limit counters in the database, shared by all API workers.

The login, email code, authenticator attempt and password lockout limits were counted in each
API process's memory, so with 2 workers each was counted per worker, and a restart cleared them.
They are now fixed-window counters in this table. Identity data like sessions: no clinic_id and
no row level security. Keys are HMACs, so no IP or email address is stored.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.create_table(
        "rate_counters",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_counters")),
    )
    # DELETE: the password lockout is cleared on a correct password, and the worker prunes
    # expired windows.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON rate_counters TO app_rw")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE migrator")
    op.drop_table("rate_counters")
    op.execute("RESET ROLE")

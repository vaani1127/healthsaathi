"""Keep the last accepted authenticator step per user in the database.

Each authenticator time step is accepted once per user. Before this revision the last accepted
step was kept in each API process's memory, so with 2 workers a code could be accepted once per
worker, and again after a restart. A code is now accepted only by an update that moves
mfa_secrets.last_step forward.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.add_column("mfa_secrets", sa.Column("last_step", sa.BigInteger(), nullable=True))
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE migrator")
    op.drop_column("mfa_secrets", "last_step")
    op.execute("RESET ROLE")

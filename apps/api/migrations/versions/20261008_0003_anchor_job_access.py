"""Let the anchor job record clinic signer keys, and find the audit event of an access quickly.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.execute("GRANT UPDATE (signer_pubkey) ON clinics TO anchor_job")
    op.execute(
        "CREATE POLICY anchor_update ON clinics FOR UPDATE TO anchor_job USING (true) WITH CHECK (true)"
    )
    # Receipts look up the audit event written for an access event.
    op.execute(
        "CREATE INDEX ix_audit_events_access_event ON audit_events "
        "((payload ->> 'access_event_id')) WHERE kind = 'access'"
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_events_access_event")
    op.execute("DROP POLICY IF EXISTS anchor_update ON clinics")
    op.execute("REVOKE UPDATE (signer_pubkey) ON clinics FROM anchor_job")

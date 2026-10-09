"""Status history of appointments, lab orders, referrals and invoices (append-only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.create_table(
        "status_events",
        sa.Column(
            "entity_type",
            sa.Enum(
                "appointment",
                "lab_order",
                "referral",
                "invoice",
                name="status_entity",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clinic_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('appointment', 'lab_order', 'referral', 'invoice')",
            name=op.f("ck_status_events_status_entity"),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], name=op.f("fk_status_events_clinic_id_clinics")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_status_events")),
        sa.UniqueConstraint("clinic_id", "id", name=op.f("uq_status_events_clinic_id_id")),
    )
    op.create_index(
        op.f("ix_status_events_clinic_id"), "status_events", ["clinic_id"], unique=False
    )
    op.create_index(
        "ix_status_events_entity", "status_events", ["clinic_id", "entity_id", "at"], unique=False
    )

    op.execute("ALTER TABLE status_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON status_events FOR ALL TO app_rw "
        "USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id())"
    )
    # Like the audit tables: the app may only add rows, and a trigger blocks every other change.
    op.execute("GRANT SELECT, INSERT ON status_events TO app_rw")
    op.execute(
        "CREATE TRIGGER status_events_append_only BEFORE UPDATE OR DELETE ON status_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
    )
    op.execute(
        "CREATE TRIGGER status_events_no_truncate BEFORE TRUNCATE ON status_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.drop_table("status_events")

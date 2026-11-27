"""Record versions: salted commitments of clinical record versions (ADR 0011).

Each committed version of a clinical record gets one row here and one `record_version` leaf in
the clinic's audit log, written in the same transaction. Like the audit tables, the app may only
add rows, and a trigger blocks every other change, even by the owner. A partial index finds the
leaves of one record.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORD_TYPES = (
    "note",
    "prescription",
    "vital",
    "allergy",
    "condition",
    "lab_order",
    "lab_result",
    "document",
)


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.create_table(
        "record_versions",
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column(
            "record_type",
            sa.Enum(
                *RECORD_TYPES,
                name="record_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prev_version_id", sa.Uuid(), nullable=True),
        sa.Column("content_schema", sa.String(length=32), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("commitment", sa.LargeBinary(), nullable=False),
        sa.Column("audit_seq", sa.BigInteger(), nullable=False),
        sa.Column("backfilled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clinic_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "record_type IN (" + ", ".join(f"'{t}'" for t in RECORD_TYPES) + ")",
            name=op.f("ck_record_versions_record_type"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_record_versions_version")),
        sa.CheckConstraint("octet_length(salt) = 32", name=op.f("ck_record_versions_salt_len")),
        sa.CheckConstraint(
            "octet_length(commitment) = 32", name=op.f("ck_record_versions_commitment_len")
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], name=op.f("fk_record_versions_clinic_id_clinics")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_record_versions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name=op.f("fk_record_versions_clinic_id_patient_id_patients"),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "prev_version_id"],
            ["record_versions.clinic_id", "record_versions.id"],
            name=op.f("fk_record_versions_clinic_id_prev_version_id_record_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record_versions")),
        sa.UniqueConstraint("clinic_id", "id", name=op.f("uq_record_versions_clinic_id_id")),
        sa.UniqueConstraint(
            "clinic_id",
            "record_type",
            "record_id",
            "version",
            name=op.f("uq_record_versions_clinic_id_record_type_record_id_version"),
        ),
    )
    op.create_index(
        op.f("ix_record_versions_clinic_id"), "record_versions", ["clinic_id"], unique=False
    )
    op.create_index(
        "ix_record_versions_record", "record_versions", ["clinic_id", "record_id"], unique=False
    )

    op.execute("ALTER TABLE record_versions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON record_versions FOR ALL TO app_rw "
        "USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id())"
    )
    op.execute("GRANT SELECT, INSERT ON record_versions TO app_rw")
    op.execute(
        "CREATE TRIGGER record_versions_append_only BEFORE UPDATE OR DELETE ON record_versions "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
    )
    op.execute(
        "CREATE TRIGGER record_versions_no_truncate BEFORE TRUNCATE ON record_versions "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
    )

    # The integrity check finds every leaf of one record, even if its record_versions row is gone.
    op.execute(
        "CREATE INDEX ix_audit_events_record_id ON audit_events "
        "((payload ->> 'record_id')) WHERE kind = 'record_version'"
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_events_record_id")
    op.drop_table("record_versions")

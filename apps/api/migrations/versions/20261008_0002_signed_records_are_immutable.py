"""Signed clinical notes and prescriptions cannot be changed or deleted.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 10:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNED_TABLES = ("clinical_notes", "prescriptions")

FUNCTION_SQL = """
CREATE FUNCTION reject_signed_change() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.signed_at IS NOT NULL THEN
        RAISE EXCEPTION '% % is signed; create a new version instead', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    op.execute("SET ROLE migrator")
    op.execute(FUNCTION_SQL)
    for table in SIGNED_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_signed_lock BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_signed_change()"
        )
    op.execute("RESET ROLE")


def downgrade() -> None:
    for table in SIGNED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_signed_lock ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_signed_change()")

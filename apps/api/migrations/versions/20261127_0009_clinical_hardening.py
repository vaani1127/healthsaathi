"""Clinical tables the app never changes become append-only (ADR 0011).

- vitals and documents: no UPDATE, DELETE or TRUNCATE for anyone (trigger), and app_rw loses its
  UPDATE grant. The code never changes these rows after insert.
- lab_results: every UPDATE and DELETE is refused except setting released_at from NULL to a value
  with every other column unchanged (trigger). app_rw may update only released_at.
- clinical_notes and prescriptions: at most one newer version per row (unique parent_id). This
  closes the race where two requests add a new version to the same parent at once.

Before the unique indexes are built, a read-only guard looks for parent_id values that already
appear twice. If it finds any, the migration stops with a clear message and changes nothing; the
duplicates must be resolved by hand first (docs/RUNBOOK.md, "Record integrity").

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27 12:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("vitals", "documents")
VERSIONED = ("clinical_notes", "prescriptions")

# Read-only. Also in docs/RUNBOOK.md so it can be run against a database before this migration.
DUPLICATE_PARENTS_SQL = """
SELECT 'clinical_notes' AS table_name, parent_id, count(*) AS children
FROM clinical_notes WHERE parent_id IS NOT NULL GROUP BY parent_id HAVING count(*) > 1
UNION ALL
SELECT 'prescriptions' AS table_name, parent_id, count(*) AS children
FROM prescriptions WHERE parent_id IS NOT NULL GROUP BY parent_id HAVING count(*) > 1
ORDER BY table_name, parent_id
"""

LAB_RESULTS_FUNCTION_SQL = """
CREATE FUNCTION lab_results_release_only() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.released_at IS NULL AND NEW.released_at IS NOT NULL
           AND (to_jsonb(NEW) - 'released_at') = (to_jsonb(OLD) - 'released_at') THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'lab_results is append-only: % is not allowed (only released_at may be set once)',
        TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;
"""


class DuplicateParentError(RuntimeError):
    pass


def check_no_duplicate_parents() -> None:
    rows = op.get_bind().execute(sa.text(DUPLICATE_PARENTS_SQL)).all()
    if rows:
        listed = "; ".join(f"{r.table_name} parent {r.parent_id} has {r.children}" for r in rows)
        raise DuplicateParentError(
            f"Migration 0009 stopped: {len(rows)} parent_id value(s) already have more than one "
            f"newer version, so the unique index cannot be built. Nothing was changed. Resolve "
            f"them by hand first (docs/RUNBOOK.md, 'Record integrity'). Found: {listed}"
        )


def upgrade() -> None:
    check_no_duplicate_parents()
    op.execute("SET ROLE migrator")
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE ON {table} FROM app_rw")
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
        )

    op.execute(LAB_RESULTS_FUNCTION_SQL)
    op.execute("REVOKE UPDATE ON lab_results FROM app_rw")
    op.execute("GRANT UPDATE (released_at) ON lab_results TO app_rw")
    op.execute(
        "CREATE TRIGGER lab_results_release_only BEFORE UPDATE OR DELETE ON lab_results "
        "FOR EACH ROW EXECUTE FUNCTION lab_results_release_only()"
    )
    op.execute(
        "CREATE TRIGGER lab_results_no_truncate BEFORE TRUNCATE ON lab_results "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
    )

    for table in VERSIONED:
        op.create_index(f"uq_{table}_parent_id", table, ["parent_id"], unique=True)
    op.execute("RESET ROLE")


def downgrade() -> None:
    for table in VERSIONED:
        op.drop_index(f"uq_{table}_parent_id", table_name=table)
    op.execute("DROP TRIGGER IF EXISTS lab_results_no_truncate ON lab_results")
    op.execute("DROP TRIGGER IF EXISTS lab_results_release_only ON lab_results")
    op.execute("DROP FUNCTION IF EXISTS lab_results_release_only()")
    op.execute("REVOKE UPDATE (released_at) ON lab_results FROM app_rw")
    op.execute("GRANT UPDATE ON lab_results TO app_rw")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"GRANT UPDATE ON {table} TO app_rw")

"""Initial schema, DB roles, row level security and append-only audit tables.

Revision ID: 0001
Revises: 
Create Date: 2026-10-08 12:23:17.435565
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables with clinic_id. Each gets a tenant isolation policy for app_rw.
CLINIC_TABLES = (
    "memberships",
    "staff_profiles",
    "patients",
    "schedules",
    "shifts",
    "appointments",
    "queue_tokens",
    "encounters",
    "referrals",
    "care_team_assignments",
    "vitals",
    "allergies",
    "conditions",
    "clinical_notes",
    "prescriptions",
    "lab_orders",
    "lab_results",
    "documents",
    "services",
    "invoices",
    "invoice_items",
    "payments",
    "consent_notices",
    "consents",
    "consent_events",
    "access_events",
    "access_explanations",
    "break_glass_events",
    "alerts",
    "alert_reviews",
    "patient_access_queries",
    "audit_events",
    "merkle_checkpoints",
    "anchor_receipts",
)

# app_rw may only add rows to these tables. A trigger also blocks UPDATE, DELETE and TRUNCATE.
APPEND_ONLY_TABLES = ("audit_events", "access_events", "access_explanations")

# Identity and system tables without clinic_id. app_rw may delete rows from these.
APP_DELETE_TABLES = ("devices", "sessions", "email_otps", "push_subscriptions", "jobs")

ROLES_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN
        CREATE ROLE migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        CREATE ROLE app_rw NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anchor_job') THEN
        CREATE ROLE anchor_job NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'researcher_ro') THEN
        CREATE ROLE researcher_ro NOLOGIN NOBYPASSRLS;
    END IF;
    IF current_user <> 'migrator' AND NOT pg_has_role(current_user, 'migrator', 'MEMBER') THEN
        EXECUTE format('GRANT migrator TO %I', current_user);
    END IF;
    EXECUTE format('GRANT CREATE ON DATABASE %I TO migrator', current_database());
END
$$;
"""

CONTEXT_FUNCTIONS_SQL = (
    """
CREATE FUNCTION app_clinic_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.clinic_id', true), '')::uuid $$;
""",
    """
CREATE FUNCTION app_user_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
""",
    """
CREATE FUNCTION app_role() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.role', true), '') $$;
""",
)

PARTITION_FUNCTION_SQL = """
CREATE FUNCTION create_month_partitions(parent text, months_back int, months_ahead int)
    RETURNS int
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
DECLARE
    base date := date_trunc('month', timezone('UTC', now()))::date;
    month_start date;
    part text;
    created int := 0;
BEGIN
    IF parent NOT IN ('access_events', 'audit_events') THEN
        RAISE EXCEPTION 'not a partitioned table: %', parent;
    END IF;
    FOR i IN -months_back..months_ahead LOOP
        month_start := (base + make_interval(months => i))::date;
        part := parent || '_' || to_char(month_start, 'YYYYMM');
        IF to_regclass(part) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part,
                parent,
                month_start::timestamp AT TIME ZONE 'UTC',
                (month_start + interval '1 month')::timestamp AT TIME ZONE 'UTC'
            );
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END
$$;
"""

APPEND_ONLY_FUNCTION_SQL = """
CREATE FUNCTION reject_append_only_change() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;
"""

TOP_LEVEL_GRANTS_SQL = """
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT c.relname FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p')
          AND NOT c.relispartition
          AND c.relname <> 'alembic_version'
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO app_rw', t);
    END LOOP;
END
$$;
"""

RESEARCH_VIEW_SQL = """
CREATE VIEW research.access_events_deid WITH (security_barrier) AS
SELECT
    ae.id,
    ae.clinic_id,
    encode(sha256(convert_to(ae.user_id::text, 'UTF8')), 'hex') AS user_pseudo,
    encode(sha256(convert_to(ae.patient_id::text, 'UTF8')), 'hex') AS patient_pseudo,
    ae.role,
    ae.resource_type,
    ae.action,
    ae.at,
    ae.decision,
    ae.policy_version,
    ae.break_glass_id IS NOT NULL AS is_break_glass,
    ex.template_code,
    ex.strength,
    ex.forgery_flags
FROM access_events ae
LEFT JOIN access_explanations ex ON ex.access_event_id = ae.id;
"""


def _create_security() -> None:
    for statement in CONTEXT_FUNCTIONS_SQL:
        op.execute(statement)
    op.execute(PARTITION_FUNCTION_SQL)
    op.execute("REVOKE ALL ON FUNCTION create_month_partitions(text, int, int) FROM PUBLIC")
    op.execute(APPEND_ONLY_FUNCTION_SQL)
    op.execute("SELECT create_month_partitions('access_events', 3, 6)")
    op.execute("SELECT create_month_partitions('audit_events', 3, 6)")

    # Row level security. Policies are written for app_rw; the owner (migrator) is not affected.
    for table in CLINIC_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        if table == "memberships":
            # A user can list their own memberships before choosing a clinic.
            op.execute(
                "CREATE POLICY tenant_read ON memberships FOR SELECT TO app_rw "
                "USING (clinic_id = app_clinic_id() OR user_id = app_user_id())"
            )
            op.execute(
                "CREATE POLICY tenant_insert ON memberships FOR INSERT TO app_rw "
                "WITH CHECK (clinic_id = app_clinic_id())"
            )
            op.execute(
                "CREATE POLICY tenant_update ON memberships FOR UPDATE TO app_rw "
                "USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id())"
            )
        else:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO app_rw "
                "USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id())"
            )

    op.execute("ALTER TABLE clinics ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY clinic_read ON clinics FOR SELECT TO app_rw USING (true)")
    op.execute(
        "CREATE POLICY clinic_insert ON clinics FOR INSERT TO app_rw "
        "WITH CHECK (app_role() = 'platform_admin')"
    )
    op.execute(
        "CREATE POLICY clinic_update ON clinics FOR UPDATE TO app_rw "
        "USING (id = app_clinic_id() OR app_role() = 'platform_admin') "
        "WITH CHECK (id = app_clinic_id() OR app_role() = 'platform_admin')"
    )

    # The anchor job works across clinics through its own policies, never through BYPASSRLS.
    op.execute("CREATE POLICY anchor_read ON clinics FOR SELECT TO anchor_job USING (true)")
    op.execute("CREATE POLICY anchor_read ON audit_events FOR SELECT TO anchor_job USING (true)")
    for table in ("merkle_checkpoints", "anchor_receipts"):
        op.execute(
            f"CREATE POLICY anchor_all ON {table} FOR ALL TO anchor_job "
            "USING (true) WITH CHECK (true)"
        )

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
        )

    op.execute("CREATE SCHEMA research")
    op.execute(RESEARCH_VIEW_SQL)

    # Grants.
    op.execute("GRANT USAGE ON SCHEMA public TO app_rw, anchor_job")
    # Grant on top level tables only. Partitions get no grants, so they can only be read
    # through the parent table, where the RLS policies apply.
    op.execute(TOP_LEVEL_GRANTS_SQL)
    op.execute("REVOKE UPDATE ON " + ", ".join(APPEND_ONLY_TABLES) + " FROM app_rw")
    op.execute("REVOKE INSERT, UPDATE ON merkle_checkpoints FROM app_rw")
    op.execute("REVOKE INSERT, UPDATE ON anchor_receipts FROM app_rw")
    op.execute("GRANT INSERT ON merkle_checkpoints TO app_rw")
    op.execute("GRANT DELETE ON " + ", ".join(APP_DELETE_TABLES) + " TO app_rw")
    op.execute("GRANT USAGE ON SEQUENCE audit_events_seq_seq TO app_rw")
    op.execute(
        "GRANT EXECUTE ON FUNCTION create_month_partitions(text, int, int) TO app_rw, anchor_job"
    )

    op.execute("GRANT SELECT ON clinics, audit_events TO anchor_job")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON merkle_checkpoints, anchor_receipts TO anchor_job"
    )

    op.execute("GRANT USAGE ON SCHEMA research TO researcher_ro")
    op.execute("GRANT SELECT ON research.access_events_deid TO researcher_ro")


def _drop_security() -> None:
    op.execute("DROP SCHEMA IF EXISTS research CASCADE")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")


def upgrade() -> None:
    op.execute(ROLES_SQL)
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO migrator")
    # Everything below is owned by migrator, so app roles never own the tables they use.
    op.execute("SET ROLE migrator")
    op.execute("CREATE SEQUENCE audit_events_seq_seq")
    op.create_table('clinics',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('city', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('timezone', sa.Text(), server_default='Asia/Kolkata', nullable=False),
    sa.Column('signer_pubkey', sa.LargeBinary(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clinics'))
    )
    op.create_table('email_otps',
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('code_hash', sa.Text(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_otps'))
    )
    op.create_index('ix_email_otps_email_purpose', 'email_otps', ['email', 'purpose'], unique=False)
    op.create_table('jobs',
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index('ix_jobs_pending', 'jobs', ['run_after'], unique=False, postgresql_where=sa.text('done_at IS NULL'))
    op.create_table('policy_versions',
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('yaml', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_versions')),
    sa.UniqueConstraint('sha256', name=op.f('uq_policy_versions_sha256'))
    )
    op.create_table('users',
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index('uq_users_email_lower', 'users', [sa.literal_column('lower(email)')], unique=True)
    op.create_table('access_explanations',
    sa.Column('access_event_id', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('template_code', sa.String(length=32), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('strength', sa.REAL(), nullable=False),
    sa.Column('forgery_flags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_access_explanations_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('access_event_id', name=op.f('pk_access_explanations'))
    )
    op.create_index('ix_access_explanations_clinic', 'access_explanations', ['clinic_id', 'computed_at'], unique=False)
    op.create_table('alerts',
    sa.Column('access_event_id', sa.Uuid(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('rank_in_day', sa.Integer(), nullable=False),
    sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('policy_version', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('open', 'benign', 'misuse', 'unsure', name='alert_status', native_enum=False, create_constraint=False, length=32), server_default='open', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'benign', 'misuse', 'unsure')", name=op.f('ck_alerts_alert_status')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_alerts_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alerts')),
    sa.UniqueConstraint('clinic_id', 'access_event_id', name=op.f('uq_alerts_clinic_id_access_event_id')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_alerts_clinic_id_id'))
    )
    op.create_index(op.f('ix_alerts_clinic_id'), 'alerts', ['clinic_id'], unique=False)
    op.create_index('ix_alerts_day_rank', 'alerts', ['clinic_id', 'day', 'rank_in_day'], unique=False)
    op.create_table('audit_events',
    sa.Column('seq', sa.BigInteger(), server_default=sa.text("nextval('audit_events_seq_seq')"), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_hash', sa.LargeBinary(), nullable=False),
    sa.Column('prev_hash', sa.LargeBinary(), nullable=False),
    sa.Column('chain_hash', sa.LargeBinary(), nullable=False),
    sa.CheckConstraint('octet_length(chain_hash) = 32', name=op.f('ck_audit_events_chain_hash_len')),
    sa.CheckConstraint('octet_length(payload_hash) = 32', name=op.f('ck_audit_events_payload_hash_len')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_audit_events_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('seq', 'at', name=op.f('pk_audit_events')),
    sa.UniqueConstraint('id', 'at', name=op.f('uq_audit_events_id_at')),
    postgresql_partition_by='RANGE (at)'
    )
    op.create_index('ix_audit_events_clinic_seq', 'audit_events', ['clinic_id', 'seq'], unique=False)
    op.create_table('consent_notices',
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('text_en', sa.Text(), nullable=False),
    sa.Column('text_hi', sa.Text(), nullable=False),
    sa.Column('purposes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_consent_notices_version')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_consent_notices_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consent_notices')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_consent_notices_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'version', name=op.f('uq_consent_notices_clinic_id_version'))
    )
    op.create_index(op.f('ix_consent_notices_clinic_id'), 'consent_notices', ['clinic_id'], unique=False)
    op.create_table('devices',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('fingerprint_hash', sa.String(length=64), nullable=False),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_devices_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('user_id', 'fingerprint_hash', name=op.f('uq_devices_user_id_fingerprint_hash'))
    )
    op.create_table('memberships',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient', name='role', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient')", name=op.f('ck_memberships_role')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_memberships_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memberships')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_memberships_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'user_id', 'role', name=op.f('uq_memberships_clinic_id_user_id_role'))
    )
    op.create_index(op.f('ix_memberships_clinic_id'), 'memberships', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_table('merkle_checkpoints',
    sa.Column('tree_size', sa.BigInteger(), nullable=False),
    sa.Column('root', sa.LargeBinary(), nullable=False),
    sa.Column('prev_root', sa.LargeBinary(), nullable=True),
    sa.Column('signed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('signature', sa.LargeBinary(), nullable=False),
    sa.Column('key_id', sa.String(length=16), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('tree_size >= 0', name=op.f('ck_merkle_checkpoints_tree_size')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_merkle_checkpoints_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merkle_checkpoints')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_merkle_checkpoints_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'tree_size', name=op.f('uq_merkle_checkpoints_clinic_id_tree_size'))
    )
    op.create_index(op.f('ix_merkle_checkpoints_clinic_id'), 'merkle_checkpoints', ['clinic_id'], unique=False)
    op.create_table('mfa_secrets',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('totp_secret_enc', sa.LargeBinary(), nullable=False),
    sa.Column('enabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_mfa_secrets_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_mfa_secrets'))
    )
    op.create_table('patients',
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('mrn', sa.String(length=32), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('dob', sa.Date(), nullable=True),
    sa.Column('sex', sa.Enum('female', 'male', 'other', 'unknown', name='sex', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('abha_number', sa.String(length=32), nullable=True),
    sa.Column('emergency_contact', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("sex IN ('female', 'male', 'other', 'unknown')", name=op.f('ck_patients_sex')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_patients_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_patients_created_by_users')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_patients_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_patients')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_patients_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'mrn', name=op.f('uq_patients_clinic_id_mrn'))
    )
    op.create_index(op.f('ix_patients_clinic_id'), 'patients', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_patients_user_id'), 'patients', ['user_id'], unique=False)
    op.create_table('push_subscriptions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('endpoint', sa.Text(), nullable=False),
    sa.Column('p256dh', sa.Text(), nullable=False),
    sa.Column('auth', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_push_subscriptions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_push_subscriptions')),
    sa.UniqueConstraint('endpoint', name=op.f('uq_push_subscriptions_endpoint'))
    )
    op.create_index(op.f('ix_push_subscriptions_user_id'), 'push_subscriptions', ['user_id'], unique=False)
    op.create_table('schedules',
    sa.Column('doctor_user_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('slot_minutes', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('active', 'inactive', name='record_status', native_enum=False, create_constraint=False, length=32), server_default='active', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f('ck_schedules_record_status')),
    sa.CheckConstraint('end_time > start_time', name=op.f('ck_schedules_time_order')),
    sa.CheckConstraint('slot_minutes BETWEEN 5 AND 120', name=op.f('ck_schedules_slot_minutes')),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name=op.f('ck_schedules_weekday')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_schedules_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_schedules_created_by_users')),
    sa.ForeignKeyConstraint(['doctor_user_id'], ['users.id'], name=op.f('fk_schedules_doctor_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_schedules')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_schedules_clinic_id_id'))
    )
    op.create_index(op.f('ix_schedules_clinic_id'), 'schedules', ['clinic_id'], unique=False)
    op.create_table('services',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('price_paise', sa.BigInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('price_paise >= 0', name=op.f('ck_services_price')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_services_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_services')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_services_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'name', name=op.f('uq_services_clinic_id_name'))
    )
    op.create_index(op.f('ix_services_clinic_id'), 'services', ['clinic_id'], unique=False)
    op.create_table('shifts',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('role', sa.Enum('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient', name='role', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('scheduled', 'cancelled', name='shift_status', native_enum=False, create_constraint=False, length=32), server_default='scheduled', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient')", name=op.f('ck_shifts_role')),
    sa.CheckConstraint("status IN ('scheduled', 'cancelled')", name=op.f('ck_shifts_shift_status')),
    sa.CheckConstraint('ends_at > starts_at', name=op.f('ck_shifts_time_order')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_shifts_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_shifts_created_by_users')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_shifts_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shifts')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_shifts_clinic_id_id'))
    )
    op.create_index(op.f('ix_shifts_clinic_id'), 'shifts', ['clinic_id'], unique=False)
    op.create_index('ix_shifts_user_starts', 'shifts', ['user_id', 'starts_at'], unique=False)
    op.create_table('staff_profiles',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('registration_no', sa.Text(), nullable=True),
    sa.Column('specialization', sa.Text(), nullable=True),
    sa.Column('department', sa.Text(), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_staff_profiles_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_staff_profiles_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_staff_profiles')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_staff_profiles_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'user_id', name=op.f('uq_staff_profiles_clinic_id_user_id'))
    )
    op.create_index(op.f('ix_staff_profiles_clinic_id'), 'staff_profiles', ['clinic_id'], unique=False)
    op.create_table('alert_reviews',
    sa.Column('alert_id', sa.Uuid(), nullable=False),
    sa.Column('reviewer_user_id', sa.Uuid(), nullable=False),
    sa.Column('outcome', sa.Enum('benign', 'misuse', 'unsure', name='outcome', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("outcome IN ('benign', 'misuse', 'unsure')", name=op.f('ck_alert_reviews_outcome')),
    sa.ForeignKeyConstraint(['clinic_id', 'alert_id'], ['alerts.clinic_id', 'alerts.id'], name=op.f('fk_alert_reviews_clinic_id_alert_id_alerts')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_alert_reviews_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id'], name=op.f('fk_alert_reviews_reviewer_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_reviews')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_alert_reviews_clinic_id_id'))
    )
    op.create_index(op.f('ix_alert_reviews_alert_id'), 'alert_reviews', ['alert_id'], unique=False)
    op.create_index(op.f('ix_alert_reviews_clinic_id'), 'alert_reviews', ['clinic_id'], unique=False)
    op.create_table('allergies',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('substance', sa.Text(), nullable=False),
    sa.Column('reaction', sa.Text(), nullable=True),
    sa.Column('severity', sa.Enum('mild', 'moderate', 'severe', name='severity', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('recorded_by', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("severity IN ('mild', 'moderate', 'severe')", name=op.f('ck_allergies_severity')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_allergies_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_allergies_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['recorded_by'], ['users.id'], name=op.f('fk_allergies_recorded_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_allergies')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_allergies_clinic_id_id'))
    )
    op.create_index(op.f('ix_allergies_clinic_id'), 'allergies', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_allergies_patient_id'), 'allergies', ['patient_id'], unique=False)
    op.create_table('anchor_receipts',
    sa.Column('checkpoint_id', sa.Uuid(), nullable=False),
    sa.Column('backend', sa.Enum('amoy', 'ots', 'github', 'besu', name='anchor_backend', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('pending', 'confirmed', 'failed', name='anchor_status', native_enum=False, create_constraint=False, length=32), server_default='pending', nullable=False),
    sa.Column('tx_ref', sa.Text(), nullable=True),
    sa.Column('block_ref', sa.Text(), nullable=True),
    sa.Column('proof', sa.LargeBinary(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("backend IN ('amoy', 'ots', 'github', 'besu')", name=op.f('ck_anchor_receipts_anchor_backend')),
    sa.CheckConstraint("status IN ('pending', 'confirmed', 'failed')", name=op.f('ck_anchor_receipts_anchor_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'checkpoint_id'], ['merkle_checkpoints.clinic_id', 'merkle_checkpoints.id'], name=op.f('fk_anchor_receipts_clinic_id_checkpoint_id_merkle_checkpoints')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_anchor_receipts_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_anchor_receipts')),
    sa.UniqueConstraint('checkpoint_id', 'backend', name=op.f('uq_anchor_receipts_checkpoint_id_backend')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_anchor_receipts_clinic_id_id'))
    )
    op.create_index(op.f('ix_anchor_receipts_clinic_id'), 'anchor_receipts', ['clinic_id'], unique=False)
    op.create_table('appointments',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('doctor_user_id', sa.Uuid(), nullable=False),
    sa.Column('slot_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('slot_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.Enum('new', 'followup', 'walkin', name='appointment_kind', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('booked', 'checked_in', 'in_consult', 'completed', 'cancelled', 'no_show', name='appointment_status', native_enum=False, create_constraint=False, length=32), server_default='booked', nullable=False),
    sa.Column('source', sa.Enum('reception', 'patient', 'doctor', name='appointment_source', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('new', 'followup', 'walkin')", name=op.f('ck_appointments_appointment_kind')),
    sa.CheckConstraint("source IN ('reception', 'patient', 'doctor')", name=op.f('ck_appointments_appointment_source')),
    sa.CheckConstraint("status IN ('booked', 'checked_in', 'in_consult', 'completed', 'cancelled', 'no_show')", name=op.f('ck_appointments_appointment_status')),
    sa.CheckConstraint('slot_end > slot_start', name=op.f('ck_appointments_time_order')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_appointments_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_appointments_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_appointments_created_by_users')),
    sa.ForeignKeyConstraint(['doctor_user_id'], ['users.id'], name=op.f('fk_appointments_doctor_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_appointments')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_appointments_clinic_id_id'))
    )
    op.create_index(op.f('ix_appointments_clinic_id'), 'appointments', ['clinic_id'], unique=False)
    op.create_index('ix_appointments_doctor_slot', 'appointments', ['doctor_user_id', 'slot_start'], unique=False)
    op.create_index('ix_appointments_patient', 'appointments', ['clinic_id', 'patient_id'], unique=False)
    op.create_table('break_glass_events',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('reason_code', sa.String(length=32), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.Enum('benign', 'misuse', 'unsure', name='outcome', native_enum=False, create_constraint=False, length=32), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("outcome IN ('benign', 'misuse', 'unsure')", name=op.f('ck_break_glass_events_outcome')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_break_glass_events_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_break_glass_events_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_break_glass_events_reviewed_by_users')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_break_glass_events_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_break_glass_events')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_break_glass_events_clinic_id_id'))
    )
    op.create_index(op.f('ix_break_glass_events_clinic_id'), 'break_glass_events', ['clinic_id'], unique=False)
    op.create_index('ix_break_glass_review', 'break_glass_events', ['clinic_id', 'reviewed_at'], unique=False)
    op.create_table('care_team_assignments',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient', name='role', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.Enum('active', 'inactive', name='record_status', native_enum=False, create_constraint=False, length=32), server_default='active', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient')", name=op.f('ck_care_team_assignments_role')),
    sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f('ck_care_team_assignments_record_status')),
    sa.CheckConstraint('ends_at IS NULL OR ends_at > starts_at', name=op.f('ck_care_team_assignments_time_order')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_care_team_assignments_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_care_team_assignments_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_care_team_assignments_created_by_users')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_care_team_assignments_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_care_team_assignments')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_care_team_assignments_clinic_id_id'))
    )
    op.create_index(op.f('ix_care_team_assignments_clinic_id'), 'care_team_assignments', ['clinic_id'], unique=False)
    op.create_index('ix_care_team_user_patient', 'care_team_assignments', ['user_id', 'patient_id'], unique=False)
    op.create_table('conditions',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('onset', sa.Date(), nullable=True),
    sa.Column('status', sa.Enum('active', 'resolved', 'inactive', name='condition_status', native_enum=False, create_constraint=False, length=32), server_default='active', nullable=False),
    sa.Column('recorded_by', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'resolved', 'inactive')", name=op.f('ck_conditions_condition_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_conditions_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_conditions_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['recorded_by'], ['users.id'], name=op.f('fk_conditions_recorded_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conditions')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_conditions_clinic_id_id'))
    )
    op.create_index(op.f('ix_conditions_clinic_id'), 'conditions', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_conditions_patient_id'), 'conditions', ['patient_id'], unique=False)
    op.create_table('consents',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('notice_id', sa.Uuid(), nullable=False),
    sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('channel', sa.Enum('reception', 'patient_portal', name='consent_channel', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('salt_hash', sa.String(length=64), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("channel IN ('reception', 'patient_portal')", name=op.f('ck_consents_consent_channel')),
    sa.ForeignKeyConstraint(['clinic_id', 'notice_id'], ['consent_notices.clinic_id', 'consent_notices.id'], name=op.f('fk_consents_clinic_id_notice_id_consent_notices')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_consents_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_consents_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consents')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_consents_clinic_id_id'))
    )
    op.create_index(op.f('ix_consents_clinic_id'), 'consents', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_consents_patient_id'), 'consents', ['patient_id'], unique=False)
    op.create_table('documents',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('blob_path', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_documents_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_documents_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_documents_uploaded_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_documents_clinic_id_id'))
    )
    op.create_index(op.f('ix_documents_clinic_id'), 'documents', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_documents_patient_id'), 'documents', ['patient_id'], unique=False)
    op.create_table('patient_access_queries',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('access_event_id', sa.Uuid(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('open', 'answered', 'closed', name='query_status', native_enum=False, create_constraint=False, length=32), server_default='open', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'answered', 'closed')", name=op.f('ck_patient_access_queries_query_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_patient_access_queries_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_patient_access_queries_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_patient_access_queries')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_patient_access_queries_clinic_id_id'))
    )
    op.create_index(op.f('ix_patient_access_queries_clinic_id'), 'patient_access_queries', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_patient_access_queries_patient_id'), 'patient_access_queries', ['patient_id'], unique=False)
    op.create_table('referrals',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('from_user_id', sa.Uuid(), nullable=False),
    sa.Column('to_user_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('active', 'completed', 'cancelled', name='referral_status', native_enum=False, create_constraint=False, length=32), server_default='active', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'completed', 'cancelled')", name=op.f('ck_referrals_referral_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_referrals_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_referrals_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_referrals_created_by_users')),
    sa.ForeignKeyConstraint(['from_user_id'], ['users.id'], name=op.f('fk_referrals_from_user_id_users')),
    sa.ForeignKeyConstraint(['to_user_id'], ['users.id'], name=op.f('fk_referrals_to_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_referrals')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_referrals_clinic_id_id'))
    )
    op.create_index(op.f('ix_referrals_clinic_id'), 'referrals', ['clinic_id'], unique=False)
    op.create_index('ix_referrals_to_user', 'referrals', ['to_user_id', 'patient_id'], unique=False)
    op.create_table('sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=True),
    sa.Column('refresh_hash', sa.String(length=64), nullable=False),
    sa.Column('family_id', sa.Uuid(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_sessions_device_id_devices')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions')),
    sa.UniqueConstraint('refresh_hash', name=op.f('uq_sessions_refresh_hash'))
    )
    op.create_index(op.f('ix_sessions_family_id'), 'sessions', ['family_id'], unique=False)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('access_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient', name='role', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('resource_type', sa.Enum('demographics', 'vitals', 'allergies', 'notes', 'prescriptions', 'lab', 'billing', 'documents', 'consent', name='resource_type', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('action', sa.Enum('view', 'create', 'edit', 'export', 'print', name='action', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=True),
    sa.Column('device_id', sa.Uuid(), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('policy_version', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.Enum('allow', 'deny', name='decision', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('break_glass_id', sa.Uuid(), nullable=True),
    sa.CheckConstraint("action IN ('view', 'create', 'edit', 'export', 'print')", name=op.f('ck_access_events_action')),
    sa.CheckConstraint("decision IN ('allow', 'deny')", name=op.f('ck_access_events_decision')),
    sa.CheckConstraint("resource_type IN ('demographics', 'vitals', 'allergies', 'notes', 'prescriptions', 'lab', 'billing', 'documents', 'consent')", name=op.f('ck_access_events_resource_type')),
    sa.CheckConstraint("role IN ('platform_admin', 'clinic_admin', 'reception', 'nurse', 'doctor', 'lab_tech', 'patient')", name=op.f('ck_access_events_role')),
    sa.ForeignKeyConstraint(['clinic_id', 'break_glass_id'], ['break_glass_events.clinic_id', 'break_glass_events.id'], name=op.f('fk_access_events_clinic_id_break_glass_id_break_glass_events')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_access_events_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_access_events_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_access_events_device_id_devices')),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], name=op.f('fk_access_events_session_id_sessions')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_access_events_user_id_users')),
    sa.PrimaryKeyConstraint('id', 'at', name=op.f('pk_access_events')),
    postgresql_partition_by='RANGE (at)'
    )
    op.create_index('ix_access_events_clinic_at', 'access_events', ['clinic_id', 'at'], unique=False)
    op.create_index('ix_access_events_patient_at', 'access_events', ['clinic_id', 'patient_id', 'at'], unique=False)
    op.create_index('ix_access_events_user_at', 'access_events', ['user_id', 'at'], unique=False)
    op.create_table('consent_events',
    sa.Column('consent_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('granted', 'withdrawn', name='consent_event_kind', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('by_user_id', sa.Uuid(), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("kind IN ('granted', 'withdrawn')", name=op.f('ck_consent_events_consent_event_kind')),
    sa.ForeignKeyConstraint(['by_user_id'], ['users.id'], name=op.f('fk_consent_events_by_user_id_users')),
    sa.ForeignKeyConstraint(['clinic_id', 'consent_id'], ['consents.clinic_id', 'consents.id'], name=op.f('fk_consent_events_clinic_id_consent_id_consents')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_consent_events_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consent_events')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_consent_events_clinic_id_id'))
    )
    op.create_index(op.f('ix_consent_events_clinic_id'), 'consent_events', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_consent_events_consent_id'), 'consent_events', ['consent_id'], unique=False)
    op.create_table('encounters',
    sa.Column('appointment_id', sa.Uuid(), nullable=True),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('doctor_user_id', sa.Uuid(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.Enum('open', 'closed', name='encounter_status', native_enum=False, create_constraint=False, length=32), server_default='open', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'closed')", name=op.f('ck_encounters_encounter_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'appointment_id'], ['appointments.clinic_id', 'appointments.id'], name=op.f('fk_encounters_clinic_id_appointment_id_appointments')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_encounters_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_encounters_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_encounters_created_by_users')),
    sa.ForeignKeyConstraint(['doctor_user_id'], ['users.id'], name=op.f('fk_encounters_doctor_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_encounters')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_encounters_clinic_id_id'))
    )
    op.create_index(op.f('ix_encounters_clinic_id'), 'encounters', ['clinic_id'], unique=False)
    op.create_index('ix_encounters_patient', 'encounters', ['clinic_id', 'patient_id'], unique=False)
    op.create_table('invoices',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('draft', 'issued', 'paid', 'void', name='invoice_status', native_enum=False, create_constraint=False, length=32), server_default='draft', nullable=False),
    sa.Column('total_paise', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'issued', 'paid', 'void')", name=op.f('ck_invoices_invoice_status')),
    sa.CheckConstraint('total_paise >= 0', name=op.f('ck_invoices_total')),
    sa.ForeignKeyConstraint(['clinic_id', 'appointment_id'], ['appointments.clinic_id', 'appointments.id'], name=op.f('fk_invoices_clinic_id_appointment_id_appointments')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_invoices_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_invoices_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_invoices_created_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invoices')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_invoices_clinic_id_id'))
    )
    op.create_index(op.f('ix_invoices_clinic_id'), 'invoices', ['clinic_id'], unique=False)
    op.create_index('ix_invoices_patient', 'invoices', ['clinic_id', 'patient_id'], unique=False)
    op.create_table('queue_tokens',
    sa.Column('appointment_id', sa.Uuid(), nullable=False),
    sa.Column('token_no', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('status', sa.Enum('waiting', 'with_nurse', 'with_doctor', 'done', 'skipped', name='queue_status', native_enum=False, create_constraint=False, length=32), server_default='waiting', nullable=False),
    sa.Column('assigned_nurse_user_id', sa.Uuid(), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('waiting', 'with_nurse', 'with_doctor', 'done', 'skipped')", name=op.f('ck_queue_tokens_queue_status')),
    sa.ForeignKeyConstraint(['assigned_nurse_user_id'], ['users.id'], name=op.f('fk_queue_tokens_assigned_nurse_user_id_users')),
    sa.ForeignKeyConstraint(['clinic_id', 'appointment_id'], ['appointments.clinic_id', 'appointments.id'], name=op.f('fk_queue_tokens_clinic_id_appointment_id_appointments')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_queue_tokens_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_queue_tokens_created_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_queue_tokens')),
    sa.UniqueConstraint('clinic_id', 'appointment_id', name=op.f('uq_queue_tokens_clinic_id_appointment_id')),
    sa.UniqueConstraint('clinic_id', 'day', 'token_no', name=op.f('uq_queue_tokens_clinic_id_day_token_no')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_queue_tokens_clinic_id_id'))
    )
    op.create_index(op.f('ix_queue_tokens_clinic_id'), 'queue_tokens', ['clinic_id'], unique=False)
    op.create_table('clinical_notes',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('encounter_id', sa.Uuid(), nullable=False),
    sa.Column('author_user_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('body_enc', sa.LargeBinary(), nullable=False),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_clinical_notes_version')),
    sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], name=op.f('fk_clinical_notes_author_user_id_users')),
    sa.ForeignKeyConstraint(['clinic_id', 'encounter_id'], ['encounters.clinic_id', 'encounters.id'], name=op.f('fk_clinical_notes_clinic_id_encounter_id_encounters')),
    sa.ForeignKeyConstraint(['clinic_id', 'parent_id'], ['clinical_notes.clinic_id', 'clinical_notes.id'], name=op.f('fk_clinical_notes_clinic_id_parent_id_clinical_notes')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_clinical_notes_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_clinical_notes_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clinical_notes')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_clinical_notes_clinic_id_id'))
    )
    op.create_index(op.f('ix_clinical_notes_clinic_id'), 'clinical_notes', ['clinic_id'], unique=False)
    op.create_index('ix_clinical_notes_patient', 'clinical_notes', ['clinic_id', 'patient_id'], unique=False)
    op.create_table('invoice_items',
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('qty', sa.Integer(), server_default='1', nullable=False),
    sa.Column('price_paise', sa.BigInteger(), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('price_paise >= 0', name=op.f('ck_invoice_items_price')),
    sa.CheckConstraint('qty > 0', name=op.f('ck_invoice_items_qty')),
    sa.ForeignKeyConstraint(['clinic_id', 'invoice_id'], ['invoices.clinic_id', 'invoices.id'], name=op.f('fk_invoice_items_clinic_id_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['clinic_id', 'service_id'], ['services.clinic_id', 'services.id'], name=op.f('fk_invoice_items_clinic_id_service_id_services')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_invoice_items_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invoice_items')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_invoice_items_clinic_id_id'))
    )
    op.create_index(op.f('ix_invoice_items_clinic_id'), 'invoice_items', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_invoice_items_invoice_id'), 'invoice_items', ['invoice_id'], unique=False)
    op.create_table('lab_orders',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('encounter_id', sa.Uuid(), nullable=True),
    sa.Column('ordered_by', sa.Uuid(), nullable=False),
    sa.Column('tests', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('ordered', 'collected', 'resulted', 'cancelled', name='lab_order_status', native_enum=False, create_constraint=False, length=32), server_default='ordered', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ordered', 'collected', 'resulted', 'cancelled')", name=op.f('ck_lab_orders_lab_order_status')),
    sa.ForeignKeyConstraint(['clinic_id', 'encounter_id'], ['encounters.clinic_id', 'encounters.id'], name=op.f('fk_lab_orders_clinic_id_encounter_id_encounters')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_lab_orders_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_lab_orders_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['ordered_by'], ['users.id'], name=op.f('fk_lab_orders_ordered_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lab_orders')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_lab_orders_clinic_id_id'))
    )
    op.create_index(op.f('ix_lab_orders_clinic_id'), 'lab_orders', ['clinic_id'], unique=False)
    op.create_index('ix_lab_orders_patient', 'lab_orders', ['clinic_id', 'patient_id'], unique=False)
    op.create_index('ix_lab_orders_status', 'lab_orders', ['clinic_id', 'status'], unique=False)
    op.create_table('payments',
    sa.Column('invoice_id', sa.Uuid(), nullable=False),
    sa.Column('method', sa.Enum('cash', 'upi', 'card_offline', name='payment_method', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('amount_paise', sa.BigInteger(), nullable=False),
    sa.Column('received_by', sa.Uuid(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("method IN ('cash', 'upi', 'card_offline')", name=op.f('ck_payments_payment_method')),
    sa.CheckConstraint('amount_paise > 0', name=op.f('ck_payments_amount')),
    sa.ForeignKeyConstraint(['clinic_id', 'invoice_id'], ['invoices.clinic_id', 'invoices.id'], name=op.f('fk_payments_clinic_id_invoice_id_invoices')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_payments_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['received_by'], ['users.id'], name=op.f('fk_payments_received_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_payments_clinic_id_id'))
    )
    op.create_index(op.f('ix_payments_clinic_id'), 'payments', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_payments_invoice_id'), 'payments', ['invoice_id'], unique=False)
    op.create_table('prescriptions',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('encounter_id', sa.Uuid(), nullable=False),
    sa.Column('author_user_id', sa.Uuid(), nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('advice_en', sa.Text(), nullable=True),
    sa.Column('advice_hi', sa.Text(), nullable=True),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_prescriptions_version')),
    sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], name=op.f('fk_prescriptions_author_user_id_users')),
    sa.ForeignKeyConstraint(['clinic_id', 'encounter_id'], ['encounters.clinic_id', 'encounters.id'], name=op.f('fk_prescriptions_clinic_id_encounter_id_encounters')),
    sa.ForeignKeyConstraint(['clinic_id', 'parent_id'], ['prescriptions.clinic_id', 'prescriptions.id'], name=op.f('fk_prescriptions_clinic_id_parent_id_prescriptions')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_prescriptions_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_prescriptions_clinic_id_clinics')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_prescriptions')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_prescriptions_clinic_id_id'))
    )
    op.create_index(op.f('ix_prescriptions_clinic_id'), 'prescriptions', ['clinic_id'], unique=False)
    op.create_index('ix_prescriptions_patient', 'prescriptions', ['clinic_id', 'patient_id'], unique=False)
    op.create_table('vitals',
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('encounter_id', sa.Uuid(), nullable=True),
    sa.Column('recorded_by', sa.Uuid(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('bp_sys', sa.SmallInteger(), nullable=True),
    sa.Column('bp_dia', sa.SmallInteger(), nullable=True),
    sa.Column('pulse', sa.SmallInteger(), nullable=True),
    sa.Column('temp_c', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.Column('spo2', sa.SmallInteger(), nullable=True),
    sa.Column('rr', sa.SmallInteger(), nullable=True),
    sa.Column('weight_kg', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('height_cm', sa.Numeric(precision=5, scale=1), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id', 'encounter_id'], ['encounters.clinic_id', 'encounters.id'], name=op.f('fk_vitals_clinic_id_encounter_id_encounters')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_vitals_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_vitals_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['recorded_by'], ['users.id'], name=op.f('fk_vitals_recorded_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vitals')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_vitals_clinic_id_id'))
    )
    op.create_index(op.f('ix_vitals_clinic_id'), 'vitals', ['clinic_id'], unique=False)
    op.create_index('ix_vitals_patient_time', 'vitals', ['clinic_id', 'patient_id', 'recorded_at'], unique=False)
    op.create_table('lab_results',
    sa.Column('lab_order_id', sa.Uuid(), nullable=False),
    sa.Column('patient_id', sa.Uuid(), nullable=False),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=True),
    sa.Column('resulted_by', sa.Uuid(), nullable=False),
    sa.Column('resulted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clinic_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['clinic_id', 'document_id'], ['documents.clinic_id', 'documents.id'], name=op.f('fk_lab_results_clinic_id_document_id_documents')),
    sa.ForeignKeyConstraint(['clinic_id', 'lab_order_id'], ['lab_orders.clinic_id', 'lab_orders.id'], name=op.f('fk_lab_results_clinic_id_lab_order_id_lab_orders')),
    sa.ForeignKeyConstraint(['clinic_id', 'patient_id'], ['patients.clinic_id', 'patients.id'], name=op.f('fk_lab_results_clinic_id_patient_id_patients')),
    sa.ForeignKeyConstraint(['clinic_id'], ['clinics.id'], name=op.f('fk_lab_results_clinic_id_clinics')),
    sa.ForeignKeyConstraint(['resulted_by'], ['users.id'], name=op.f('fk_lab_results_resulted_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lab_results')),
    sa.UniqueConstraint('clinic_id', 'id', name=op.f('uq_lab_results_clinic_id_id')),
    sa.UniqueConstraint('clinic_id', 'lab_order_id', name=op.f('uq_lab_results_clinic_id_lab_order_id'))
    )
    op.create_index(op.f('ix_lab_results_clinic_id'), 'lab_results', ['clinic_id'], unique=False)
    op.create_index(op.f('ix_lab_results_patient_id'), 'lab_results', ['patient_id'], unique=False)
    _create_security()
    op.execute("RESET ROLE")


def downgrade() -> None:
    _drop_security()
    op.drop_index(op.f('ix_lab_results_patient_id'), table_name='lab_results')
    op.drop_index(op.f('ix_lab_results_clinic_id'), table_name='lab_results')
    op.drop_table('lab_results')
    op.drop_index('ix_vitals_patient_time', table_name='vitals')
    op.drop_index(op.f('ix_vitals_clinic_id'), table_name='vitals')
    op.drop_table('vitals')
    op.drop_index('ix_prescriptions_patient', table_name='prescriptions')
    op.drop_index(op.f('ix_prescriptions_clinic_id'), table_name='prescriptions')
    op.drop_table('prescriptions')
    op.drop_index(op.f('ix_payments_invoice_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_clinic_id'), table_name='payments')
    op.drop_table('payments')
    op.drop_index('ix_lab_orders_status', table_name='lab_orders')
    op.drop_index('ix_lab_orders_patient', table_name='lab_orders')
    op.drop_index(op.f('ix_lab_orders_clinic_id'), table_name='lab_orders')
    op.drop_table('lab_orders')
    op.drop_index(op.f('ix_invoice_items_invoice_id'), table_name='invoice_items')
    op.drop_index(op.f('ix_invoice_items_clinic_id'), table_name='invoice_items')
    op.drop_table('invoice_items')
    op.drop_index('ix_clinical_notes_patient', table_name='clinical_notes')
    op.drop_index(op.f('ix_clinical_notes_clinic_id'), table_name='clinical_notes')
    op.drop_table('clinical_notes')
    op.drop_index(op.f('ix_queue_tokens_clinic_id'), table_name='queue_tokens')
    op.drop_table('queue_tokens')
    op.drop_index('ix_invoices_patient', table_name='invoices')
    op.drop_index(op.f('ix_invoices_clinic_id'), table_name='invoices')
    op.drop_table('invoices')
    op.drop_index('ix_encounters_patient', table_name='encounters')
    op.drop_index(op.f('ix_encounters_clinic_id'), table_name='encounters')
    op.drop_table('encounters')
    op.drop_index(op.f('ix_consent_events_consent_id'), table_name='consent_events')
    op.drop_index(op.f('ix_consent_events_clinic_id'), table_name='consent_events')
    op.drop_table('consent_events')
    op.drop_index('ix_access_events_user_at', table_name='access_events')
    op.drop_index('ix_access_events_patient_at', table_name='access_events')
    op.drop_index('ix_access_events_clinic_at', table_name='access_events')
    op.drop_table('access_events')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_family_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index('ix_referrals_to_user', table_name='referrals')
    op.drop_index(op.f('ix_referrals_clinic_id'), table_name='referrals')
    op.drop_table('referrals')
    op.drop_index(op.f('ix_patient_access_queries_patient_id'), table_name='patient_access_queries')
    op.drop_index(op.f('ix_patient_access_queries_clinic_id'), table_name='patient_access_queries')
    op.drop_table('patient_access_queries')
    op.drop_index(op.f('ix_documents_patient_id'), table_name='documents')
    op.drop_index(op.f('ix_documents_clinic_id'), table_name='documents')
    op.drop_table('documents')
    op.drop_index(op.f('ix_consents_patient_id'), table_name='consents')
    op.drop_index(op.f('ix_consents_clinic_id'), table_name='consents')
    op.drop_table('consents')
    op.drop_index(op.f('ix_conditions_patient_id'), table_name='conditions')
    op.drop_index(op.f('ix_conditions_clinic_id'), table_name='conditions')
    op.drop_table('conditions')
    op.drop_index('ix_care_team_user_patient', table_name='care_team_assignments')
    op.drop_index(op.f('ix_care_team_assignments_clinic_id'), table_name='care_team_assignments')
    op.drop_table('care_team_assignments')
    op.drop_index('ix_break_glass_review', table_name='break_glass_events')
    op.drop_index(op.f('ix_break_glass_events_clinic_id'), table_name='break_glass_events')
    op.drop_table('break_glass_events')
    op.drop_index('ix_appointments_patient', table_name='appointments')
    op.drop_index('ix_appointments_doctor_slot', table_name='appointments')
    op.drop_index(op.f('ix_appointments_clinic_id'), table_name='appointments')
    op.drop_table('appointments')
    op.drop_index(op.f('ix_anchor_receipts_clinic_id'), table_name='anchor_receipts')
    op.drop_table('anchor_receipts')
    op.drop_index(op.f('ix_allergies_patient_id'), table_name='allergies')
    op.drop_index(op.f('ix_allergies_clinic_id'), table_name='allergies')
    op.drop_table('allergies')
    op.drop_index(op.f('ix_alert_reviews_clinic_id'), table_name='alert_reviews')
    op.drop_index(op.f('ix_alert_reviews_alert_id'), table_name='alert_reviews')
    op.drop_table('alert_reviews')
    op.drop_index(op.f('ix_staff_profiles_clinic_id'), table_name='staff_profiles')
    op.drop_table('staff_profiles')
    op.drop_index('ix_shifts_user_starts', table_name='shifts')
    op.drop_index(op.f('ix_shifts_clinic_id'), table_name='shifts')
    op.drop_table('shifts')
    op.drop_index(op.f('ix_services_clinic_id'), table_name='services')
    op.drop_table('services')
    op.drop_index(op.f('ix_schedules_clinic_id'), table_name='schedules')
    op.drop_table('schedules')
    op.drop_index(op.f('ix_push_subscriptions_user_id'), table_name='push_subscriptions')
    op.drop_table('push_subscriptions')
    op.drop_index(op.f('ix_patients_user_id'), table_name='patients')
    op.drop_index(op.f('ix_patients_clinic_id'), table_name='patients')
    op.drop_table('patients')
    op.drop_table('mfa_secrets')
    op.drop_index(op.f('ix_merkle_checkpoints_clinic_id'), table_name='merkle_checkpoints')
    op.drop_table('merkle_checkpoints')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_clinic_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_table('devices')
    op.drop_index(op.f('ix_consent_notices_clinic_id'), table_name='consent_notices')
    op.drop_table('consent_notices')
    op.drop_index('ix_audit_events_clinic_seq', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('ix_alerts_day_rank', table_name='alerts')
    op.drop_index(op.f('ix_alerts_clinic_id'), table_name='alerts')
    op.drop_table('alerts')
    op.drop_index('ix_access_explanations_clinic', table_name='access_explanations')
    op.drop_table('access_explanations')
    op.drop_index('uq_users_email_lower', table_name='users')
    op.drop_table('users')
    op.drop_table('policy_versions')
    op.drop_index('ix_jobs_pending', table_name='jobs', postgresql_where=sa.text('done_at IS NULL'))
    op.drop_table('jobs')
    op.drop_index('ix_email_otps_email_purpose', table_name='email_otps')
    op.drop_table('email_otps')
    op.drop_table('clinics')
    op.execute("DROP SEQUENCE IF EXISTS audit_events_seq_seq")
    op.execute("DROP FUNCTION IF EXISTS create_month_partitions(text, int, int)")
    op.execute("DROP FUNCTION IF EXISTS reject_append_only_change()")
    op.execute("DROP FUNCTION IF EXISTS app_clinic_id()")
    op.execute("DROP FUNCTION IF EXISTS app_user_id()")
    op.execute("DROP FUNCTION IF EXISTS app_role()")

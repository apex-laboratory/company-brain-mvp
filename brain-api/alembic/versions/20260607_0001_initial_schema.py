"""Initial multi-tenant SaaS schema

Revision ID: 0001
Revises:
Create Date: 2026-06-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID, INET, ARRAY, ENUM

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Custom ENUM ───────────────────────────────────────────────────────────
    op.execute("CREATE TYPE member_role AS ENUM ('owner', 'admin', 'editor', 'viewer')")

    # ── organizations ─────────────────────────────────────────────────────────
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("plan", sa.String(20), server_default=sa.text("'trial'")),
        sa.Column("plan_seats", sa.Integer, server_default=sa.text("5")),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("TRUE")),
        sa.Column("deleted_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
    )

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("auth_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255)),
        sa.Column("avatar_url", sa.String),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
    )

    # ── organization_members ──────────────────────────────────────────────────
    op.create_table(
        "organization_members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", ENUM("owner", "admin", "editor", "viewer", name="member_role", create_type=False), nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("TRUE")),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("joined_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("org_id", "user_id"),
    )
    op.create_index(None, "organization_members", ["org_id", "user_id"])
    op.create_index(None, "organization_members", ["user_id"])

    # ── invitations ───────────────────────────────────────────────────────────
    op.create_table(
        "invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", ENUM("owner", "admin", "editor", "viewer", name="member_role", create_type=False), nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False, server_default=sa.text("NOW() + INTERVAL '7 days'")),
        sa.Column("accepted_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "invitations", ["org_id", "email"])

    # ── source_connections ────────────────────────────────────────────────────
    op.create_table(
        "source_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'connected'")),
        sa.Column("access_token_enc", sa.LargeBinary),
        sa.Column("refresh_token_enc", sa.LargeBinary),
        sa.Column("token_expires_at", sa.DateTime),
        sa.Column("scopes", ARRAY(sa.Text)),
        sa.Column("external_account_id", sa.String(255)),
        sa.Column("monitored_ids", JSONB, server_default=sa.text("'[]'")),
        sa.Column("lookback_days", sa.Integer, server_default=sa.text("180")),
        sa.Column("connected_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("connected_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("org_id", "source", "external_account_id"),
    )
    op.create_index(None, "source_connections", ["org_id", "source", "status"])

    # ── webhook_subscriptions ─────────────────────────────────────────────────
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_ref_id", sa.String(255)),
        sa.Column("target_id", sa.String(255)),
        sa.Column("secret_enc", sa.LargeBinary),
        sa.Column("status", sa.String(20), server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "webhook_subscriptions", ["org_id", "source", "status"])

    # ── skills ────────────────────────────────────────────────────────────────
    op.create_table(
        "skills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer, server_default=sa.text("1")),
        sa.Column("trigger", sa.Text),
        sa.Column("base_logic", sa.Text),
        sa.Column("exceptions_block", JSONB, server_default=sa.text("'[]'")),
        sa.Column("actions", JSONB, server_default=sa.text("'[]'")),
        sa.Column("source_ids", JSONB, server_default=sa.text("'[]'")),
        sa.Column("source_authority", sa.String(10)),
        sa.Column("conflict_flags", JSONB, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'")),
        sa.Column("confidence", sa.Float),
        sa.Column("embedding", Vector(1536)),
        sa.Column("changed_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("deleted_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("org_id", "name"),
    )
    # HNSW index — must be raw SQL; Alembic doesn't support the USING hnsw syntax
    op.execute("CREATE INDEX skills_embedding_hnsw ON skills USING hnsw (embedding vector_cosine_ops)")
    op.create_index(None, "skills", ["org_id", "status"])
    op.create_index(None, "skills", ["org_id", "source_authority"])

    # ── skill_versions ────────────────────────────────────────────────────────
    op.create_table(
        "skill_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("base_logic", sa.Text),
        sa.Column("exceptions_block", JSONB),
        sa.Column("confidence", sa.Float),
        sa.Column("changed_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("change_type", sa.String(30)),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "skill_versions", ["org_id", "skill_id"])

    # ── review_queue ──────────────────────────────────────────────────────────
    op.create_table(
        "review_queue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("review_type", sa.String(30), nullable=False),
        sa.Column("proposed_update", JSONB),
        sa.Column("source_a", JSONB),
        sa.Column("source_b", JSONB),
        sa.Column("confidence", sa.Float),
        sa.Column("reason", sa.Text),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'")),
        sa.Column("resolved_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("resolved_at", sa.DateTime),
    )
    op.create_index(None, "review_queue", ["org_id", "status"])
    op.create_index(None, "review_queue", ["org_id", "review_type"])

    # ── source_events ─────────────────────────────────────────────────────────
    op.create_table(
        "source_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255)),
        sa.Column("external_event_id", sa.String(255)),
        sa.Column("payload", JSONB),
        sa.Column("processed", sa.Boolean, server_default=sa.text("FALSE")),
        sa.Column("skill_id", UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("outcome", sa.String(30)),
        sa.Column("sweep_id", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("org_id", "source", "external_event_id"),
    )
    op.create_index(None, "source_events", ["org_id", "source", "processed"])
    op.create_index(None, "source_events", ["org_id", "sweep_id"])

    # ── sweeps ────────────────────────────────────────────────────────────────
    op.create_table(
        "sweeps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'running'")),
        sa.Column("config", JSONB),
        sa.Column("progress", JSONB, server_default=sa.text("'{}'")),
        sa.Column("skills_created", sa.Integer, server_default=sa.text("0")),
        sa.Column("skills_queued", sa.Integer, server_default=sa.text("0")),
        sa.Column("triggered_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("started_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime),
    )
    op.create_index(None, "sweeps", ["org_id", "status"])

    # ── agent_interactions ────────────────────────────────────────────────────
    op.create_table(
        "agent_interactions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("skill_id", UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("query", sa.Text),
        sa.Column("matched_confidence", sa.Float),
        sa.Column("match_type", sa.String(20)),
        sa.Column("agent_action", JSONB),
        sa.Column("human_override", sa.Boolean, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "agent_interactions", ["org_id", "user_id"])
    op.create_index(None, "agent_interactions", ["org_id", "skill_id"])

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource", sa.String(50)),
        sa.Column("resource_id", UUID(as_uuid=True)),
        sa.Column("old_value", JSONB),
        sa.Column("new_value", JSONB),
        sa.Column("ip_address", INET),
        sa.Column("user_agent", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "audit_log", ["org_id", sa.text("created_at DESC")])
    op.create_index(None, "audit_log", ["org_id", "resource", "resource_id"])

    # ── organization_settings ─────────────────────────────────────────────────
    op.create_table(
        "organization_settings",
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("max_seats", sa.Integer, server_default=sa.text("5")),
        sa.Column("max_skills", sa.Integer, server_default=sa.text("500")),
        sa.Column("max_sweeps_per_day", sa.Integer, server_default=sa.text("3")),
        sa.Column("retention_days", sa.Integer, server_default=sa.text("365")),
        sa.Column("sso_enabled", sa.Boolean, server_default=sa.text("FALSE")),
        sa.Column("sso_provider", sa.String(20)),
        sa.Column("branding", JSONB, server_default=sa.text("'{}'")),
        sa.Column("sso_config", JSONB, server_default=sa.text("'{}'")),
        sa.Column("features", JSONB, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
    )

    # ── organization_api_keys ─────────────────────────────────────────────────
    op.create_table(
        "organization_api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.LargeBinary, nullable=False),
        sa.Column("key_prefix", sa.String(10), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text), server_default=sa.text('\'{"read"}\'')),
        sa.Column("last_used_at", sa.DateTime),
        sa.Column("expires_at", sa.DateTime),
        sa.Column("revoked_at", sa.DateTime),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
    )
    op.create_index(None, "organization_api_keys", ["org_id"])
    op.create_index(None, "organization_api_keys", ["key_hash"])

    # ── organization_usage ────────────────────────────────────────────────────
    op.create_table(
        "organization_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("skills_total", sa.Integer, server_default=sa.text("0")),
        sa.Column("sweeps_run", sa.Integer, server_default=sa.text("0")),
        sa.Column("queries_total", sa.Integer, server_default=sa.text("0")),
        sa.Column("tokens_used", sa.BigInteger, server_default=sa.text("0")),
        sa.Column("embeddings_run", sa.Integer, server_default=sa.text("0")),
        sa.Column("connector_syncs", JSONB, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("org_id", "period_start"),
    )
    op.create_index(None, "organization_usage", ["org_id", sa.text("period_start DESC")])

    # ── RLS: enable on all tables ─────────────────────────────────────────────
    for table in [
        "organizations", "organization_members", "invitations",
        "source_connections", "webhook_subscriptions",
        "skills", "skill_versions", "review_queue",
        "source_events", "sweeps", "agent_interactions", "audit_log",
        "organization_settings", "organization_api_keys", "organization_usage",
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # ── RLS helper functions ──────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION current_org_id()
        RETURNS UUID AS $$
          SELECT (current_setting('request.jwt.claims', true)::jsonb ->> 'org_id')::UUID;
        $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION current_member_role()
        RETURNS member_role AS $$
          SELECT om.role
          FROM organization_members om
          JOIN users u ON u.id = om.user_id
          WHERE om.org_id = current_org_id()
            AND u.auth_id = auth.uid()
            AND om.is_active = TRUE
          LIMIT 1;
        $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    """)

    # ── RLS policies ──────────────────────────────────────────────────────────
    op.execute("CREATE POLICY org_select ON organizations FOR SELECT USING (id = current_org_id())")

    op.execute("CREATE POLICY members_select ON organization_members FOR SELECT USING (org_id = current_org_id())")
    op.execute("""CREATE POLICY members_insert ON organization_members FOR INSERT
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")
    op.execute("""CREATE POLICY members_update ON organization_members FOR UPDATE
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("""CREATE POLICY invitations_select ON invitations FOR SELECT
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")
    op.execute("""CREATE POLICY invitations_write ON invitations FOR INSERT
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("""CREATE POLICY connections_select ON source_connections FOR SELECT
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")
    op.execute("""CREATE POLICY connections_write ON source_connections FOR ALL
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("""CREATE POLICY webhooks_policy ON webhook_subscriptions FOR ALL
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("CREATE POLICY skills_select ON skills FOR SELECT USING (org_id = current_org_id())")
    op.execute("""CREATE POLICY skills_insert ON skills FOR INSERT
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin', 'editor'))""")
    op.execute("""CREATE POLICY skills_update ON skills FOR UPDATE
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin', 'editor'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin', 'editor'))""")

    op.execute("CREATE POLICY skill_versions_select ON skill_versions FOR SELECT USING (org_id = current_org_id())")

    op.execute("CREATE POLICY review_select ON review_queue FOR SELECT USING (org_id = current_org_id())")
    op.execute("""CREATE POLICY review_update ON review_queue FOR UPDATE
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin', 'editor'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin', 'editor'))""")

    op.execute("""CREATE POLICY events_policy ON source_events FOR ALL
        USING (org_id = current_org_id()) WITH CHECK (org_id = current_org_id())""")
    op.execute("""CREATE POLICY sweeps_policy ON sweeps FOR ALL
        USING (org_id = current_org_id()) WITH CHECK (org_id = current_org_id())""")
    op.execute("""CREATE POLICY interactions_policy ON agent_interactions FOR ALL
        USING (org_id = current_org_id()) WITH CHECK (org_id = current_org_id())""")

    op.execute("CREATE POLICY audit_select ON audit_log FOR SELECT USING (org_id = current_org_id())")
    op.execute("CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK (org_id = current_org_id())")

    op.execute("""CREATE POLICY settings_select ON organization_settings FOR SELECT USING (org_id = current_org_id())""")
    op.execute("""CREATE POLICY settings_write ON organization_settings FOR ALL
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("""CREATE POLICY api_keys_select ON organization_api_keys FOR SELECT
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")
    op.execute("""CREATE POLICY api_keys_write ON organization_api_keys FOR ALL
        USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
        WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))""")

    op.execute("CREATE POLICY usage_select ON organization_usage FOR SELECT USING (org_id = current_org_id())")


def downgrade() -> None:
    # Drop policies, functions, tables in reverse dependency order
    for policy, table in [
        ("usage_select", "organization_usage"),
        ("api_keys_write", "organization_api_keys"), ("api_keys_select", "organization_api_keys"),
        ("settings_write", "organization_settings"), ("settings_select", "organization_settings"),
        ("audit_insert", "audit_log"), ("audit_select", "audit_log"),
        ("interactions_policy", "agent_interactions"),
        ("sweeps_policy", "sweeps"),
        ("events_policy", "source_events"),
        ("review_update", "review_queue"), ("review_select", "review_queue"),
        ("skill_versions_select", "skill_versions"),
        ("skills_update", "skills"), ("skills_insert", "skills"), ("skills_select", "skills"),
        ("webhooks_policy", "webhook_subscriptions"),
        ("connections_write", "source_connections"), ("connections_select", "source_connections"),
        ("invitations_write", "invitations"), ("invitations_select", "invitations"),
        ("members_update", "organization_members"), ("members_insert", "organization_members"), ("members_select", "organization_members"),
        ("org_select", "organizations"),
    ]:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    op.execute("DROP FUNCTION IF EXISTS current_member_role()")
    op.execute("DROP FUNCTION IF EXISTS current_org_id()")

    for table in [
        "organization_usage", "organization_api_keys", "organization_settings",
        "audit_log", "agent_interactions", "sweeps", "source_events",
        "review_queue", "skill_versions", "skills",
        "webhook_subscriptions", "source_connections",
        "invitations", "organization_members", "users", "organizations",
    ]:
        op.drop_table(table)

    op.execute("DROP TYPE IF EXISTS member_role")

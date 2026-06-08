"""Unified schema redesign: organizations->workspaces, TEXT prefixed PKs, unified skill model

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-08

What this migration does:
- Drops the entire 0001/0002 AI-service schema (no prod data; dev reset is safe)
- Rebuilds as a unified schema serving both the AI layer and the REST API:
    - organizations -> workspaces  (wrk_ TEXT PK)
    - users          -> users      (usr_ TEXT PK, drop auth_id, add name/title/avatar_color)
    - organization_members -> workspace_members  (mem_ TEXT PK, drop 'owner' role)
    - invitations keep name, get TEXT PK + invite_status enum
    - organization_settings -> workspace_settings
    - organization_api_keys -> api_keys  (key_ TEXT PK, updated scopes)
    - organization_usage -> usage_periods  (TEXT PK, columns match API doc)
    - skills: TEXT PK, keep ALL AI columns + add API projection columns
    - skill_versions: TEXT PK, add description/input_schema/output_schema, version TEXT
    - source_connections: src_ TEXT PK, source->provider, add name/sync_status/health
    - webhook_subscriptions: whs_ TEXT PK, source->provider
    - source_events / sweeps / agent_interactions: keep UUID PKs (AI-internal), workspace_id TEXT FK
    - audit_log: TEXT PK
- RLS is enabled on all tables but policies come in 0007.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── Tables that existed after 0002, in safe drop order ───────────────────────
_OLD_TABLES = [
    "organization_usage", "organization_api_keys", "organization_settings",
    "audit_log", "agent_interactions", "sweeps", "source_events",
    "review_queue", "skill_versions", "skills",
    "webhook_subscriptions", "source_connections",
    "invitations", "organization_members", "users", "organizations",
]

_OLD_POLICIES = [
    ("usage_select",           "organization_usage"),
    ("api_keys_write",         "organization_api_keys"),
    ("api_keys_select",        "organization_api_keys"),
    ("settings_write",         "organization_settings"),
    ("settings_select",        "organization_settings"),
    ("audit_insert",           "audit_log"),
    ("audit_select",           "audit_log"),
    ("interactions_policy",    "agent_interactions"),
    ("sweeps_policy",          "sweeps"),
    ("events_policy",          "source_events"),
    ("review_update",          "review_queue"),
    ("review_select",          "review_queue"),
    ("skill_versions_select",  "skill_versions"),
    ("skills_update",          "skills"),
    ("skills_insert",          "skills"),
    ("skills_select",          "skills"),
    ("webhooks_policy",        "webhook_subscriptions"),
    ("connections_write",      "source_connections"),
    ("connections_select",     "source_connections"),
    ("invitations_write",      "invitations"),
    ("invitations_select",     "invitations"),
    ("members_update",         "organization_members"),
    ("members_insert",         "organization_members"),
    ("members_select",         "organization_members"),
    ("org_select",             "organizations"),
]


def upgrade() -> None:
    # ── 1. Tear down old schema ───────────────────────────────────────────────
    for table in _OLD_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS {table} NO FORCE ROW LEVEL SECURITY")

    for policy, table in _OLD_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    op.execute("DROP FUNCTION IF EXISTS current_member_role() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS current_org_id() CASCADE")

    for table in _OLD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP TYPE IF EXISTS member_role")

    # ── 2. Extensions ─────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # ── 3. Enums ──────────────────────────────────────────────────────────────
    op.execute("CREATE TYPE workspace_plan  AS ENUM ('trial','starter','pro','enterprise')")
    op.execute("CREATE TYPE member_role     AS ENUM ('admin','editor','viewer')")
    op.execute("CREATE TYPE invite_status   AS ENUM ('pending','accepted','revoked','expired')")
    op.execute("CREATE TYPE source_provider AS ENUM ('slack','notion','github','jira','zendesk')")
    op.execute("CREATE TYPE source_status   AS ENUM ('connected','disconnected','error','pending')")
    op.execute("CREATE TYPE sync_status     AS ENUM ('healthy','pending','syncing','error')")
    op.execute("CREATE TYPE skill_status    AS ENUM ('stable','active','draft','review')")

    # ── 4. workspaces (tenant root) ───────────────────────────────────────────
    op.create_table(
        "workspaces",
        sa.Column("id",               sa.Text, primary_key=True),          # wrk_…
        sa.Column("name",             sa.Text, nullable=False),
        sa.Column("slug",             sa.Text, nullable=False, unique=True),
        sa.Column("domain",           sa.Text),
        sa.Column("plan",             sa.Enum("trial", "starter", "pro", "enterprise",
                                              name="workspace_plan", create_type=False),
                                      nullable=False, server_default=sa.text("'trial'")),
        sa.Column("seat_limit",       sa.Integer, nullable=False, server_default=sa.text("5")),
        sa.Column("team_size",        sa.Text),                            # '1-10'|'11-50'|…
        sa.Column("primary_use_case", sa.Text),                            # 'support'|'ops'|…
        sa.Column("onboarding_step",  sa.Text),
        sa.Column("created_by",       sa.Text),                            # FK added after users
        sa.Column("deleted_at",       sa.DateTime(timezone=True)),
        sa.Column("created_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
    )

    # ── 5. users (global) ─────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",            sa.Text, primary_key=True),             # usr_…
        sa.Column("email",         sa.Text, nullable=False, unique=True),  # CITEXT via check; unique
        sa.Column("name",          sa.Text),
        sa.Column("title",         sa.Text),
        sa.Column("avatar_color",  sa.Text),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at",    sa.DateTime(timezone=True),
                                   nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",    sa.DateTime(timezone=True),
                                   nullable=False, server_default=sa.text("now()")),
    )
    # Case-insensitive email uniqueness enforced via functional index
    op.execute("CREATE UNIQUE INDEX users_email_lower ON users (lower(email))")

    # Add FK from workspaces.created_by -> users.id now that users exists
    op.create_foreign_key(None, "workspaces", "users", ["created_by"], ["id"])

    # ── 6. workspace_members ──────────────────────────────────────────────────
    op.create_table(
        "workspace_members",
        sa.Column("id",           sa.Text, primary_key=True),              # mem_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("user_id",      sa.Text,
                                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("role",         sa.Enum("admin", "editor", "viewer",
                                          name="member_role", create_type=False),
                                  nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("is_active",    sa.Boolean, nullable=False, server_default=sa.text("TRUE")),
        sa.Column("invited_by",   sa.Text, sa.ForeignKey("users.id")),
        sa.Column("joined_at",    sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "user_id",
                            name="workspace_members_workspace_id_user_id_key"),
    )
    op.create_index(None, "workspace_members", ["workspace_id", "user_id"])
    op.create_index(None, "workspace_members", ["user_id"])

    # ── 7. invitations ────────────────────────────────────────────────────────
    op.create_table(
        "invitations",
        sa.Column("id",           sa.Text, primary_key=True),              # inv_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("email",        sa.Text, nullable=False),
        sa.Column("role",         sa.Enum("admin", "editor", "viewer",
                                          name="member_role", create_type=False),
                                  nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("token_hash",   sa.LargeBinary, nullable=False, unique=True),
        sa.Column("status",       sa.Enum("pending", "accepted", "revoked", "expired",
                                          name="invite_status", create_type=False),
                                  nullable=False, server_default=sa.text("'pending'")),
        sa.Column("invited_by",   sa.Text,
                                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at",   sa.DateTime(timezone=True), nullable=False,
                                  server_default=sa.text("now() + INTERVAL '7 days'")),
        sa.Column("accepted_at",  sa.DateTime(timezone=True)),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "invitations", ["workspace_id", "email"])

    # ── 8. workspace_settings ─────────────────────────────────────────────────
    op.create_table(
        "workspace_settings",
        sa.Column("workspace_id",       sa.Text,
                                        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                        primary_key=True),
        sa.Column("max_seats",          sa.Integer, server_default=sa.text("5")),
        sa.Column("max_skills",         sa.Integer, server_default=sa.text("500")),
        sa.Column("max_sweeps_per_day", sa.Integer, server_default=sa.text("3")),
        sa.Column("retention_days",     sa.Integer, server_default=sa.text("365")),
        sa.Column("sso_enabled",        sa.Boolean, server_default=sa.text("FALSE")),
        sa.Column("sso_provider",       sa.Text),                          # 'saml'|'oidc'
        sa.Column("branding",           JSONB, server_default=sa.text("'{}'")),
        sa.Column("sso_config",         JSONB, server_default=sa.text("'{}'")),
        sa.Column("features",           JSONB, server_default=sa.text("'{}'")),
        sa.Column("updated_at",         sa.DateTime(timezone=True),
                                        nullable=False, server_default=sa.text("now()")),
    )

    # ── 9. skills (unified: AI columns + API projection columns) ──────────────
    op.create_table(
        "skills",
        sa.Column("id",               sa.Text, primary_key=True),          # skl_…
        sa.Column("workspace_id",     sa.Text,
                                      sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                      nullable=False),
        # ── API projection columns ──────────────────────────────────────────
        sa.Column("name",             sa.Text, nullable=False),
        sa.Column("version",          sa.Text, nullable=False,
                                      server_default=sa.text("'v1'")),     # 'v1','v4'…
        sa.Column("description",      sa.Text),
        sa.Column("status",           sa.Enum("stable", "active", "draft", "review",
                                              name="skill_status", create_type=False),
                                      nullable=False, server_default=sa.text("'draft'")),
        sa.Column("source_providers", ARRAY(sa.Text),
                                      nullable=False, server_default=sa.text("'{}'")),
        sa.Column("input_schema",     JSONB),
        sa.Column("output_schema",    JSONB),
        sa.Column("calls_30d",        sa.Integer,
                                      nullable=False, server_default=sa.text("0")),
        # ── AI service columns ──────────────────────────────────────────────
        sa.Column("trigger",          sa.Text),
        sa.Column("base_logic",       sa.Text),
        sa.Column("exceptions_block", JSONB, server_default=sa.text("'[]'")),
        sa.Column("actions",          JSONB, server_default=sa.text("'[]'")),
        sa.Column("source_ids",       JSONB, server_default=sa.text("'[]'")),
        sa.Column("source_authority", sa.Text),                            # 'high'|'medium'|'low'
        sa.Column("conflict_flags",   JSONB, server_default=sa.text("'[]'")),
        sa.Column("confidence",       sa.Float),
        sa.Column("embedding",        Vector(1536)),
        # ── Shared ──────────────────────────────────────────────────────────
        sa.Column("changed_by",       sa.Text, sa.ForeignKey("users.id")),
        sa.Column("deleted_at",       sa.DateTime(timezone=True)),
        sa.Column("created_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "name", name="skills_workspace_id_name_key"),
    )
    op.execute(
        "CREATE INDEX skills_embedding_hnsw ON skills USING hnsw (embedding vector_cosine_ops)"
    )
    op.create_index(None, "skills", ["workspace_id", "status"])
    op.create_index(None, "skills", ["workspace_id", "source_authority"])
    op.execute(
        "CREATE INDEX skills_name_search ON skills "
        "USING gin (to_tsvector('english', coalesce(name,'') || ' ' || coalesce(description,'')))"
    )

    # ── 10. skill_versions ────────────────────────────────────────────────────
    op.create_table(
        "skill_versions",
        sa.Column("id",               sa.Text, primary_key=True),
        sa.Column("workspace_id",     sa.Text,
                                      sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                      nullable=False),
        sa.Column("skill_id",         sa.Text,
                                      sa.ForeignKey("skills.id", ondelete="CASCADE"),
                                      nullable=False),
        sa.Column("version",          sa.Text, nullable=False),            # 'v1','v2'…
        sa.Column("description",      sa.Text),
        sa.Column("base_logic",       sa.Text),
        sa.Column("exceptions_block", JSONB),
        sa.Column("input_schema",     JSONB),
        sa.Column("output_schema",    JSONB),
        sa.Column("confidence",       sa.Float),
        sa.Column("changed_by",       sa.Text, sa.ForeignKey("users.id")),
        sa.Column("change_type",      sa.Text),  # create|update|human_edit|sweep_sourced
        sa.Column("created_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "skill_versions", ["workspace_id", "skill_id"])

    # ── 11. source_connections ────────────────────────────────────────────────
    op.create_table(
        "source_connections",
        sa.Column("id",                   sa.Text, primary_key=True),      # src_…
        sa.Column("workspace_id",         sa.Text,
                                          sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                          nullable=False),
        sa.Column("provider",             sa.Enum("slack", "notion", "github",
                                                  "jira", "zendesk",
                                                  name="source_provider", create_type=False),
                                          nullable=False),
        sa.Column("name",                 sa.Text, nullable=False),        # 'Slack workspace'
        sa.Column("status",               sa.Enum("connected", "disconnected",
                                                  "error", "pending",
                                                  name="source_status", create_type=False),
                                          nullable=False, server_default=sa.text("'pending'")),
        sa.Column("sync_status",          sa.Enum("healthy", "pending",
                                                  "syncing", "error",
                                                  name="sync_status", create_type=False),
                                          nullable=False, server_default=sa.text("'pending'")),
        sa.Column("access_token_enc",     sa.LargeBinary),
        sa.Column("refresh_token_enc",    sa.LargeBinary),
        sa.Column("token_expires_at",     sa.DateTime(timezone=True)),
        sa.Column("scopes",               ARRAY(sa.Text),
                                          nullable=False, server_default=sa.text("'{}'")),
        sa.Column("external_account_id",  sa.Text),
        sa.Column("lookback_days",        sa.Integer,
                                          nullable=False, server_default=sa.text("90")),
        sa.Column("health",               sa.Integer),                     # 0–100
        sa.Column("last_synced_at",       sa.DateTime(timezone=True)),
        sa.Column("connected_by",         sa.Text, sa.ForeignKey("users.id")),
        sa.Column("created_at",           sa.DateTime(timezone=True),
                                          nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",           sa.DateTime(timezone=True),
                                          nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "provider", "external_account_id",
                            name="source_connections_workspace_id_provider_account_key"),
    )
    op.create_index(None, "source_connections", ["workspace_id", "provider", "status"])

    # ── 12. webhook_subscriptions ─────────────────────────────────────────────
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id",            sa.Text, primary_key=True),             # whs_…
        sa.Column("workspace_id",  sa.Text,
                                   sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                   nullable=False),
        sa.Column("provider",      sa.Enum("slack", "notion", "github",
                                           "jira", "zendesk",
                                           name="source_provider", create_type=False),
                                   nullable=False),
        sa.Column("source_ref_id", sa.Text),
        sa.Column("target_id",     sa.Text),
        sa.Column("secret_enc",    sa.LargeBinary),
        sa.Column("status",        sa.Text,
                                   nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at",    sa.DateTime(timezone=True),
                                   nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "webhook_subscriptions", ["workspace_id", "provider", "status"])

    # ── 13. source_events (AI-internal, UUID PK) ──────────────────────────────
    op.create_table(
        "source_events",
        sa.Column("id",                UUID(as_uuid=True), primary_key=True,
                                       server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id",      sa.Text,
                                       sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                       nullable=False),
        sa.Column("provider",          sa.Text, nullable=False),
        sa.Column("event_type",        sa.Text, nullable=False),
        sa.Column("source_id",         sa.Text),
        sa.Column("external_event_id", sa.Text),
        sa.Column("payload",           JSONB),
        sa.Column("processed",         sa.Boolean,
                                       nullable=False, server_default=sa.text("FALSE")),
        sa.Column("skill_id",          sa.Text, sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("outcome",           sa.Text),
        sa.Column("sweep_id",          UUID(as_uuid=True)),
        sa.Column("created_at",        sa.DateTime(timezone=True),
                                       nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "provider", "external_event_id",
                            name="source_events_workspace_provider_event_key"),
    )
    op.create_index(None, "source_events", ["workspace_id", "provider", "processed"])
    op.create_index(None, "source_events", ["workspace_id", "sweep_id"])

    # ── 14. sweeps (AI-internal, UUID PK) ────────────────────────────────────
    op.create_table(
        "sweeps",
        sa.Column("id",           UUID(as_uuid=True), primary_key=True,
                                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("status",       sa.Text,
                                  nullable=False, server_default=sa.text("'running'")),
        sa.Column("config",       JSONB),
        sa.Column("progress",     JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("skills_created", sa.Integer,
                                    nullable=False, server_default=sa.text("0")),
        sa.Column("skills_queued",  sa.Integer,
                                    nullable=False, server_default=sa.text("0")),
        sa.Column("triggered_by", sa.Text, sa.ForeignKey("users.id")),
        sa.Column("started_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(None, "sweeps", ["workspace_id", "status"])

    # ── 15. agent_interactions (AI-internal, UUID PK) ────────────────────────
    op.create_table(
        "agent_interactions",
        sa.Column("id",                UUID(as_uuid=True), primary_key=True,
                                       server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id",      sa.Text,
                                       sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                       nullable=False),
        sa.Column("user_id",           sa.Text, sa.ForeignKey("users.id")),
        sa.Column("skill_id",          sa.Text,
                                       sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("query",             sa.Text),
        sa.Column("matched_confidence", sa.Float),
        sa.Column("match_type",        sa.Text),                           # semantic|query_driven|no_match
        sa.Column("agent_action",      JSONB),
        sa.Column("human_override",    sa.Boolean,
                                       nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at",        sa.DateTime(timezone=True),
                                       nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "agent_interactions", ["workspace_id", "user_id"])
    op.create_index(None, "agent_interactions", ["workspace_id", "skill_id"])

    # ── 16. api_keys ──────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id",           sa.Text, primary_key=True),              # key_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("name",         sa.Text, nullable=False),
        sa.Column("key_hash",     sa.LargeBinary, nullable=False),         # sha256; lookup index below
        sa.Column("key_prefix",   sa.Text, nullable=False),                # 'hph_live_abc1'
        sa.Column("scopes",       ARRAY(sa.Text),
                                  nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at",   sa.DateTime(timezone=True)),
        sa.Column("revoked_at",   sa.DateTime(timezone=True)),
        sa.Column("created_by",   sa.Text,
                                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "api_keys", ["workspace_id"])
    op.create_index(None, "api_keys", ["key_hash"])

    # ── 17. usage_periods ─────────────────────────────────────────────────────
    op.create_table(
        "usage_periods",
        sa.Column("id",                 sa.Text, primary_key=True),
        sa.Column("workspace_id",       sa.Text,
                                        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                        nullable=False),
        sa.Column("period_start",       sa.Date, nullable=False),
        sa.Column("period_end",         sa.Date, nullable=False),
        sa.Column("brain_queries",      sa.Integer,
                                        nullable=False, server_default=sa.text("0")),
        sa.Column("brain_query_limit",  sa.Integer),                       # NULL = unlimited
        sa.Column("mcp_calls",          sa.Integer,
                                        nullable=False, server_default=sa.text("0")),
        sa.Column("skills_served",      sa.Integer,
                                        nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_used",        sa.BigInteger,
                                        nullable=False, server_default=sa.text("0")),
        sa.Column("spark",              JSONB),                            # precomputed sparkline arrays
        sa.Column("created_at",         sa.DateTime(timezone=True),
                                        nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",         sa.DateTime(timezone=True),
                                        nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "period_start",
                            name="usage_periods_workspace_id_period_start_key"),
    )
    op.create_index(None, "usage_periods",
                    ["workspace_id", sa.text("period_start DESC")])

    # ── 18. audit_log ─────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id",           sa.Text, primary_key=True),              # act_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id"),
                                  nullable=False),
        sa.Column("user_id",      sa.Text, sa.ForeignKey("users.id")),
        sa.Column("action",       sa.Text, nullable=False),
        sa.Column("resource",     sa.Text),
        sa.Column("resource_id",  sa.Text),                                # TEXT (prefixed IDs)
        sa.Column("old_value",    JSONB),
        sa.Column("new_value",    JSONB),
        sa.Column("ip_address",   INET),
        sa.Column("user_agent",   sa.Text),
        sa.Column("request_id",   sa.Text),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "audit_log", ["workspace_id", sa.text("created_at DESC")])
    op.create_index(None, "audit_log", ["workspace_id", "resource", "resource_id"])

    # ── 19. Enable RLS (policies come in 0007) ────────────────────────────────
    for table in [
        "workspaces", "users", "workspace_members", "invitations", "workspace_settings",
        "skills", "skill_versions",
        "source_connections", "webhook_subscriptions",
        "source_events", "sweeps", "agent_interactions",
        "api_keys", "usage_periods", "audit_log",
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Drop all tables created in this migration in reverse dependency order.
    # Note: this leaves the DB without 0001/0002 tables; run a full DB reset
    # and re-apply from 0001 if you need to restore the old AI-service schema.
    for table in [
        "audit_log", "usage_periods", "api_keys",
        "agent_interactions", "sweeps", "source_events",
        "webhook_subscriptions", "source_connections",
        "skill_versions", "skills",
        "workspace_settings", "invitations", "workspace_members", "users", "workspaces",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP TYPE IF EXISTS skill_status")
    op.execute("DROP TYPE IF EXISTS sync_status")
    op.execute("DROP TYPE IF EXISTS source_status")
    op.execute("DROP TYPE IF EXISTS source_provider")
    op.execute("DROP TYPE IF EXISTS invite_status")
    op.execute("DROP TYPE IF EXISTS member_role")
    op.execute("DROP TYPE IF EXISTS workspace_plan")

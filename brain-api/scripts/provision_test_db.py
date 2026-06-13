"""Provision a local test database from the ORM models (KAN-2 integration test).

The alembic migration chain doesn't apply on this SQLAlchemy version (a
pre-existing enum-creation bug in 0001), so for an isolated end-to-end test we
build the schema directly from ``Base.metadata`` — the same models the app's
repositories target — and add the RLS GUC functions + the source-table policies
from migration 0007. Run once against an empty DB.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

import models.orm  # noqa: F401 — registers all tables on Base.metadata
from config import settings
from models.orm.base import Base

ENUMS: dict[str, list[str]] = {
    "workspace_plan": ["trial", "starter", "pro", "enterprise"],
    "source_provider": ["slack", "notion", "github", "jira", "zendesk"],
    "build_status": ["queued", "running", "completed", "failed", "canceled"],
    "decision_status": ["approved", "active", "review"],
    "member_role": ["admin", "editor", "viewer"],
    "invite_status": ["pending", "accepted", "revoked", "expired"],
    "skill_status": ["stable", "active", "draft", "review"],
    "source_status": ["connected", "disconnected", "error", "pending"],
    "sync_status": ["healthy", "pending", "syncing", "error"],
    "message_role": ["user", "assistant"],
    "review_kind": ["policy_change", "new_decision", "contradiction", "exception"],
    "review_status": ["pending", "approved", "rejected"],
}

GUC_FUNCTIONS = [
    """CREATE OR REPLACE FUNCTION current_workspace_id() RETURNS TEXT AS $$
         SELECT nullif(current_setting('app.current_workspace_id', true), '')
       $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public""",
    """CREATE OR REPLACE FUNCTION current_member_role() RETURNS member_role AS $$
         SELECT nullif(current_setting('app.current_role', true), '')::member_role
       $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public""",
    """CREATE OR REPLACE FUNCTION current_user_id() RETURNS TEXT AS $$
         SELECT nullif(current_setting('app.current_user_id', true), '')
       $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public""",
]

# Source-tier RLS policies (subset of migration 0007 relevant to KAN-2).
POLICIES = [
    ("source_connections", "connections_admin", "FOR ALL",
     "workspace_id = current_workspace_id() AND current_member_role() = 'admin'"),
    ("source_channels", "channels_select", "FOR SELECT",
     "workspace_id = current_workspace_id()"),
    ("source_channels", "channels_write", "FOR ALL",
     "workspace_id = current_workspace_id() AND current_member_role() = 'admin'"),
    ("webhook_subscriptions", "webhooks_admin", "FOR ALL",
     "workspace_id = current_workspace_id() AND current_member_role() = 'admin'"),
    ("source_events", "events_workspace", "FOR ALL",
     "workspace_id = current_workspace_id()"),
    ("sweeps", "sweeps_workspace", "FOR ALL",
     "workspace_id = current_workspace_id()"),
]
RLS_TABLES = ["source_connections", "source_channels", "webhook_subscriptions",
              "source_events", "sweeps"]


def sync_url() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def main() -> None:
    engine = create_engine(sync_url())
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        for name, values in ENUMS.items():
            labels = ", ".join(f"'{v}'" for v in values)
            conn.execute(text(
                f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({labels}); "
                f"EXCEPTION WHEN duplicate_object THEN null; END $$"
            ))

    # checkfirst=True so the (pre-created) enums are detected and not recreated.
    Base.metadata.create_all(engine, checkfirst=True)

    with engine.begin() as conn:
        for fn in GUC_FUNCTIONS:
            conn.execute(text(fn))
        for table in RLS_TABLES:
            conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        for table, name, command, using in POLICIES:
            conn.execute(text(f"DROP POLICY IF EXISTS {name} ON {table}"))
            check = f"WITH CHECK ({using})" if command == "FOR ALL" else ""
            conn.execute(text(
                f"CREATE POLICY {name} ON {table} {command} USING ({using}) {check}"
            ))

    # A non-superuser role so RLS is actually enforced (the cb superuser bypasses
    # it). Mirrors production, where the app connects as an unprivileged role.
    with engine.begin() as conn:
        conn.execute(text(
            "DO $$ BEGIN CREATE ROLE brain_app LOGIN PASSWORD 'brain_app'; "
            "EXCEPTION WHEN duplicate_object THEN null; END $$"
        ))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO brain_app"))
        conn.execute(text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO brain_app"
        ))
        conn.execute(text(
            "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO brain_app"
        ))

    print("provisioned: tables + enums + GUC functions + source RLS policies + brain_app role")


if __name__ == "__main__":
    main()

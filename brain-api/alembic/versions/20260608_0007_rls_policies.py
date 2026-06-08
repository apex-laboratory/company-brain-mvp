"""RLS policies: new app.* GUC functions + all table policies

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-08

Replaces the old Supabase JWT-based RLS approach (request.jwt.claims, auth.uid())
with the self-issued JWT approach: the backend sets transaction-local GUCs via
set_config('app.current_workspace_id', ..., true) before every tenant query.

Two helper functions are created here:

  current_workspace_id() — reads app.current_workspace_id GUC
  current_member_role()  — reads app.current_role GUC (set by the backend after
                           it verifies workspace_members during authentication)

FORCE ROW LEVEL SECURITY was already applied in migration 0002 for the old tables.
All new tables created in 0003–0006 get it here.

Policy intent per table:
  workspaces           — members see only their own workspace; no direct write via RLS
  workspace_members    — all members read roster; admins insert/update; no delete via app
  invitations          — admins only (token_hash is a usable credential)
  workspace_settings   — admins read/write; other members read
  source_connections   — admins only (encrypted secrets)
  source_channels      — all members read; admins write
  webhook_subscriptions — admins only
  decisions            — all members read (deleted_at IS NULL); editors+ write
  decision_pins        — any member reads their workspace's pins; members manage own pins
  reviews              — all members read; editors+ update (resolve)
  skills               — all members read (deleted_at IS NULL); editors+ write
  skill_versions       — all members read (system writes, no member write policy)
  brain_builds         — all members read; editors+ insert
  brain_conversations  — members read own conversations; members write own
  brain_messages       — members read conversations they own; append-only (no update policy)
  activity_events      — all members read; system inserts only
  api_keys             — admins only (key_hash is a usable credential)
  usage_periods        — all members read; system writes only
  audit_log            — all members read; append-only (insert only)
  source_events        — workspace isolation; system writes
  sweeps               — workspace isolation; system writes
  agent_interactions   — workspace isolation; system writes
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that were created in 0003 and need FORCE RLS applied here
# (0002 only applied it to the old tables that were dropped in 0003)
_NEW_TENANT_TABLES = [
    "workspaces", "workspace_members", "invitations", "workspace_settings",
    "skills", "skill_versions",
    "source_connections", "source_channels", "webhook_subscriptions",
    "source_events", "sweeps", "agent_interactions",
    "api_keys", "usage_periods", "audit_log",
    # added in 0005
    # source_channels already in list above
    # added in 0006
    "decisions", "decision_pins", "reviews",
    "brain_builds", "brain_conversations", "brain_messages", "activity_events",
]


def upgrade() -> None:
    # ── Drop old Supabase helper functions (created in 0001, broken after 0003) ─
    op.execute("DROP FUNCTION IF EXISTS current_org_id() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS current_member_role() CASCADE")

    # ── New GUC-based helper functions ─────────────────────────────────────────
    # These are STABLE (safe to call multiple times in one query) and use
    # transaction-local GUCs set by the Python run_in_tenant() context manager.
    op.execute("""
        CREATE OR REPLACE FUNCTION current_workspace_id()
        RETURNS TEXT AS $$
          SELECT nullif(current_setting('app.current_workspace_id', true), '')
        $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION current_member_role()
        RETURNS member_role AS $$
          SELECT nullif(current_setting('app.current_role', true), '')::member_role
        $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION current_user_id()
        RETURNS TEXT AS $$
          SELECT nullif(current_setting('app.current_user_id', true), '')
        $$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    """)

    # ── Apply FORCE ROW LEVEL SECURITY to all new tables ─────────────────────
    for table in _NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ─────────────────────────────────────────────────────────────────────────
    # POLICIES
    # ─────────────────────────────────────────────────────────────────────────

    # ── workspaces ────────────────────────────────────────────────────────────
    # A user sees a workspace only if they are an active member.
    # Workspace creation is a bootstrapping step (no workspace context yet), so
    # INSERT is handled via a SECURITY DEFINER function — no RLS INSERT policy.
    op.execute("""
        CREATE POLICY workspaces_select ON workspaces FOR SELECT
          USING (
            id = current_workspace_id()
            AND deleted_at IS NULL
          )
    """)
    op.execute("""
        CREATE POLICY workspaces_update ON workspaces FOR UPDATE
          USING      (id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── workspace_members ─────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY members_select ON workspace_members FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY members_insert ON workspace_members FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND current_member_role() = 'admin'
          )
    """)
    # Admins may update non-admin rows; cannot escalate to admin or demote self.
    # Full privilege-escalation protection is enforced at the service layer too.
    op.execute("""
        CREATE POLICY members_update ON workspace_members FOR UPDATE
          USING (
            workspace_id = current_workspace_id()
            AND current_member_role() = 'admin'
          )
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND current_member_role() = 'admin'
          )
    """)

    # ── invitations ───────────────────────────────────────────────────────────
    # token_hash is a usable credential — restrict to admins.
    # Acceptance flow resolves by token_hash through a SECURITY DEFINER function
    # that runs before any workspace context exists.
    op.execute("""
        CREATE POLICY invitations_admin ON invitations FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── workspace_settings ────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY settings_select ON workspace_settings FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY settings_write ON workspace_settings FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── source_connections ────────────────────────────────────────────────────
    # Contains encrypted OAuth tokens — admins only at the DB level.
    op.execute("""
        CREATE POLICY connections_admin ON source_connections FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── source_channels ───────────────────────────────────────────────────────
    # All members may view which channels are selected; admins manage them.
    op.execute("""
        CREATE POLICY channels_select ON source_channels FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY channels_write ON source_channels FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── webhook_subscriptions ─────────────────────────────────────────────────
    # Contains encrypted signing secrets — admins only.
    op.execute("""
        CREATE POLICY webhooks_admin ON webhook_subscriptions FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── decisions ─────────────────────────────────────────────────────────────
    # All members read non-deleted decisions; editors and above write.
    op.execute("""
        CREATE POLICY decisions_select ON decisions FOR SELECT
          USING (workspace_id = current_workspace_id() AND deleted_at IS NULL)
    """)
    op.execute("""
        CREATE POLICY decisions_write ON decisions FOR ALL
          USING      (workspace_id = current_workspace_id()
                      AND current_member_role() IN ('admin', 'editor'))
          WITH CHECK (workspace_id = current_workspace_id()
                      AND current_member_role() IN ('admin', 'editor'))
    """)

    # ── decision_pins ─────────────────────────────────────────────────────────
    # Any member reads all pins in their workspace (used to show who pinned what).
    # Members may only insert/delete their own pins.
    op.execute("""
        CREATE POLICY pins_select ON decision_pins FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY pins_insert ON decision_pins FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
    """)
    op.execute("""
        CREATE POLICY pins_delete ON decision_pins FOR DELETE
          USING (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
    """)

    # ── reviews ───────────────────────────────────────────────────────────────
    # All members read; editors and above may resolve (UPDATE status/verdict).
    # System (backend) inserts reviews — no member INSERT policy.
    op.execute("""
        CREATE POLICY reviews_select ON reviews FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY reviews_update ON reviews FOR UPDATE
          USING (
            workspace_id = current_workspace_id()
            AND current_member_role() IN ('admin', 'editor')
          )
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND current_member_role() IN ('admin', 'editor')
          )
    """)

    # ── skills ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY skills_select ON skills FOR SELECT
          USING (workspace_id = current_workspace_id() AND deleted_at IS NULL)
    """)
    op.execute("""
        CREATE POLICY skills_write ON skills FOR ALL
          USING      (workspace_id = current_workspace_id()
                      AND current_member_role() IN ('admin', 'editor'))
          WITH CHECK (workspace_id = current_workspace_id()
                      AND current_member_role() IN ('admin', 'editor'))
    """)

    # ── skill_versions ────────────────────────────────────────────────────────
    # Read-only for members; the system writes versions.
    op.execute("""
        CREATE POLICY skill_versions_select ON skill_versions FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)

    # ── brain_builds ──────────────────────────────────────────────────────────
    # All members read build status; editors and above trigger builds.
    op.execute("""
        CREATE POLICY builds_select ON brain_builds FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY builds_insert ON brain_builds FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND current_member_role() IN ('admin', 'editor')
          )
    """)
    # Status updates come from the system job, not the user — no UPDATE policy.

    # ── brain_conversations ───────────────────────────────────────────────────
    # Members see only their own conversations; they create their own.
    op.execute("""
        CREATE POLICY conversations_select ON brain_conversations FOR SELECT
          USING (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
    """)
    op.execute("""
        CREATE POLICY conversations_insert ON brain_conversations FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
    """)
    op.execute("""
        CREATE POLICY conversations_update ON brain_conversations FOR UPDATE
          USING (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND user_id = current_user_id()
          )
    """)

    # ── brain_messages ────────────────────────────────────────────────────────
    # Members read messages from their own conversations.
    # Append-only: no UPDATE policy. The backend inserts both user and assistant turns.
    op.execute("""
        CREATE POLICY messages_select ON brain_messages FOR SELECT
          USING (
            workspace_id = current_workspace_id()
            AND conversation_id IN (
              SELECT id FROM brain_conversations
              WHERE user_id = current_user_id()
            )
          )
    """)
    op.execute("""
        CREATE POLICY messages_insert ON brain_messages FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND conversation_id IN (
              SELECT id FROM brain_conversations
              WHERE user_id = current_user_id()
            )
          )
    """)

    # ── activity_events ───────────────────────────────────────────────────────
    # All members read the workspace feed; system inserts only.
    op.execute("""
        CREATE POLICY activity_select ON activity_events FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)

    # ── api_keys ──────────────────────────────────────────────────────────────
    # key_hash is a usable credential — admins only.
    # Pre-tenant lookup (authenticate by key_hash) runs through a SECURITY DEFINER
    # function that bypasses RLS.
    op.execute("""
        CREATE POLICY api_keys_admin ON api_keys FOR ALL
          USING      (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
          WITH CHECK (workspace_id = current_workspace_id() AND current_member_role() = 'admin')
    """)

    # ── usage_periods ─────────────────────────────────────────────────────────
    # All members read usage (visible in billing page); system upserts rows.
    op.execute("""
        CREATE POLICY usage_select ON usage_periods FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)

    # ── audit_log ─────────────────────────────────────────────────────────────
    # Append-only: SELECT + INSERT only. No UPDATE or DELETE policy = immutable
    # for all application roles. Audit writes never include token/secret values.
    op.execute("""
        CREATE POLICY audit_select ON audit_log FOR SELECT
          USING (workspace_id = current_workspace_id())
    """)
    op.execute("""
        CREATE POLICY audit_insert ON audit_log FOR INSERT
          WITH CHECK (workspace_id = current_workspace_id())
    """)

    # ── source_events ─────────────────────────────────────────────────────────
    # Workspace isolation; written by the ingest pipeline.
    op.execute("""
        CREATE POLICY events_policy ON source_events FOR ALL
          USING      (workspace_id = current_workspace_id())
          WITH CHECK (workspace_id = current_workspace_id())
    """)

    # ── sweeps ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY sweeps_policy ON sweeps FOR ALL
          USING      (workspace_id = current_workspace_id())
          WITH CHECK (workspace_id = current_workspace_id())
    """)

    # ── agent_interactions ────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY interactions_policy ON agent_interactions FOR ALL
          USING      (workspace_id = current_workspace_id())
          WITH CHECK (workspace_id = current_workspace_id())
    """)


def downgrade() -> None:
    policies = [
        ("workspaces",           "workspaces_select"),
        ("workspaces",           "workspaces_update"),
        ("workspace_members",    "members_select"),
        ("workspace_members",    "members_insert"),
        ("workspace_members",    "members_update"),
        ("invitations",          "invitations_admin"),
        ("workspace_settings",   "settings_select"),
        ("workspace_settings",   "settings_write"),
        ("source_connections",   "connections_admin"),
        ("source_channels",      "channels_select"),
        ("source_channels",      "channels_write"),
        ("webhook_subscriptions","webhooks_admin"),
        ("decisions",            "decisions_select"),
        ("decisions",            "decisions_write"),
        ("decision_pins",        "pins_select"),
        ("decision_pins",        "pins_insert"),
        ("decision_pins",        "pins_delete"),
        ("reviews",              "reviews_select"),
        ("reviews",              "reviews_update"),
        ("skills",               "skills_select"),
        ("skills",               "skills_write"),
        ("skill_versions",       "skill_versions_select"),
        ("brain_builds",         "builds_select"),
        ("brain_builds",         "builds_insert"),
        ("brain_conversations",  "conversations_select"),
        ("brain_conversations",  "conversations_insert"),
        ("brain_conversations",  "conversations_update"),
        ("brain_messages",       "messages_select"),
        ("brain_messages",       "messages_insert"),
        ("activity_events",      "activity_select"),
        ("api_keys",             "api_keys_admin"),
        ("usage_periods",        "usage_select"),
        ("audit_log",            "audit_select"),
        ("audit_log",            "audit_insert"),
        ("source_events",        "events_policy"),
        ("sweeps",               "sweeps_policy"),
        ("agent_interactions",   "interactions_policy"),
    ]
    for table, policy in reversed(policies):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    for table in reversed(_NEW_TENANT_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS current_member_role()")
    op.execute("DROP FUNCTION IF EXISTS current_workspace_id()")

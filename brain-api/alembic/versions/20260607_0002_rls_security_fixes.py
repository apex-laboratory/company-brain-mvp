"""RLS security fixes: FORCE RLS, audit integrity, privilege escalation, duplicate index, max_seats

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-07

Fixes:
- Add FORCE ROW LEVEL SECURITY so the postgres table-owner is also subject to RLS
- audit_insert: tie user_id to auth.uid() so callers cannot forge records
- members_update: admin cannot demote/touch owner rows or escalate to owner
- Drop duplicate index on organization_members (covered by UniqueConstraint)
- Drop max_seats from organization_settings (plan_seats on organizations is authoritative)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL_TABLES = [
    "organizations", "users", "organization_members", "invitations",
    "source_connections", "webhook_subscriptions", "skills", "skill_versions",
    "review_queue", "source_events", "sweeps", "agent_interactions",
    "audit_log", "organization_settings", "organization_api_keys", "organization_usage",
]


def upgrade() -> None:
    # ── #1: FORCE ROW LEVEL SECURITY ─────────────────────────────────────────
    # Without this, the postgres role (table owner) bypasses RLS even when
    # ENABLE ROW LEVEL SECURITY is set. FORCE RLS applies the policies to
    # every role including the owner, so a misconfigured session that skips
    # setting request.jwt.claims sees nothing rather than everything.
    for table in _ALL_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ── #3: Audit log integrity ───────────────────────────────────────────────
    # Old policy had no user_id constraint — any org member could INSERT a row
    # with an arbitrary user_id, forging the audit trail. New policy ties
    # user_id to the authenticated caller and requires an active role.
    op.execute("DROP POLICY IF EXISTS audit_insert ON audit_log")
    op.execute("""
        CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK (
            org_id = current_org_id()
            AND user_id = (SELECT id FROM users WHERE auth_id = auth.uid())
            AND current_member_role() IS NOT NULL
        )
    """)

    # ── #4: Remove duplicate index on organization_members ────────────────────
    # UniqueConstraint('org_id','user_id') already creates a btree index on
    # those columns. The extra op.create_index created an identical second one.
    op.execute("DROP INDEX IF EXISTS ix_organization_members_org_id_user_id")

    # ── #5: Fix privilege escalation on members_update ───────────────────────
    # Old policy: current_member_role() IN ('owner','admin') — admin could
    # UPDATE the owner's row to role='viewer' (lock-out) or their own to
    # role='owner' (escalation). New policy:
    #   USING: owner can touch any row; admin can only touch non-owner rows
    #   WITH CHECK: owner can set any role; admin cannot set role='owner'
    op.execute("DROP POLICY IF EXISTS members_update ON organization_members")
    op.execute("""
        CREATE POLICY members_update ON organization_members FOR UPDATE
            USING (
                org_id = current_org_id()
                AND (
                    current_member_role() = 'owner'
                    OR (current_member_role() = 'admin' AND role != 'owner')
                )
            )
            WITH CHECK (
                org_id = current_org_id()
                AND (
                    current_member_role() = 'owner'
                    OR role != 'owner'
                )
            )
    """)

    # ── #6: Drop max_seats from organization_settings ─────────────────────────
    # plan_seats on organizations is the single authoritative seat cap.
    # max_seats duplicated it with no enforced relationship, causing drift.
    op.drop_column("organization_settings", "max_seats")


def downgrade() -> None:
    op.add_column(
        "organization_settings",
        sa.Column("max_seats", sa.Integer, server_default=sa.text("5")),
    )

    op.execute("DROP POLICY IF EXISTS members_update ON organization_members")
    op.execute("""
        CREATE POLICY members_update ON organization_members FOR UPDATE
            USING (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
            WITH CHECK (org_id = current_org_id() AND current_member_role() IN ('owner', 'admin'))
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_organization_members_org_id_user_id "
        "ON organization_members (org_id, user_id)"
    )

    op.execute("DROP POLICY IF EXISTS audit_insert ON audit_log")
    op.execute("CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK (org_id = current_org_id())")

    for table in reversed(_ALL_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

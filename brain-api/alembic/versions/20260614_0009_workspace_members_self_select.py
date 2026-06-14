"""workspace_members: self-select RLS policy for pre-tenant membership reads

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-14

The auth flow needs to read a user's own workspace membership *before* a tenant
context exists — at sign-in and on every refresh-token rotation — to mint an
access token carrying ``workspace_id`` and ``role``.

``workspace_members`` has FORCE ROW LEVEL SECURITY, and the only SELECT policy
from 0007 (``members_select``) requires ``workspace_id = current_workspace_id()``
— a chicken-and-egg for a pre-tenant lookup that does not yet know the workspace.
Relying on the backend connection bypassing RLS to read it anyway is exactly the
coupling BACKEND_BEST_PRACTICES.md §8 warns against (tenant traffic must not use
a BYPASSRLS role).

This adds a second PERMISSIVE policy: a user may always read *their own*
membership rows (``user_id = current_user_id()``). The backend sets the
transaction-local ``app.current_user_id`` GUC before the lookup, so the read
works whether or not the connection bypasses RLS, while still leaking nothing
beyond the caller's own memberships. Mirrors the existing ``users_select`` policy
that already permits ``id = current_user_id()``.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE POLICY members_self_select ON workspace_members FOR SELECT
          USING (user_id = current_user_id())
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS members_self_select ON workspace_members")

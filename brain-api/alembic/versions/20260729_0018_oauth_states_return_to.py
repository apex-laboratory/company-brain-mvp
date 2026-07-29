"""Add return_to to oauth_states for caller-chosen post-OAuth landing pages

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-29

The frontend starts a source connect from more than one surface (onboarding wizard,
Settings → Sources) and needs the callback to land the browser back where the flow
began instead of the hardcoded ``/settings/sources``. The allowlist-validated path is
supplied at ``POST /sources/{provider}/authorize`` and must survive the provider
round trip, so it rides on the short-lived ``oauth_states`` row — bound to the
single-use state, never appended to the provider ``redirect_uri``.

Nullable: NULL means the caller sent no (or a non-allowlisted) ``returnTo`` and the
callback uses the default path. ``oauth_states`` has RLS enabled with no policies
(0008) and is reached only via the service-role connection, so no policy change is
needed for the new column.

(Authored as 0018 off main's 0017 head; the in-flight feature/phase-4-5-delivery
line carries its own 0018 — whichever merges second renumbers, per the 0012
precedent. The DDL is IF (NOT) EXISTS so environments that received the column
under either numbering apply the renumbered revision cleanly.)
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS return_to TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE oauth_states DROP COLUMN IF EXISTS return_to")

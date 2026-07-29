"""Add return_to to oauth_states for caller-chosen post-OAuth landing pages

Revision ID: 0019
Revises: 0018
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

(Renumbered from 0018 to 0019 — 0018 was claimed by both this branch and
``feat(embeddings): record vector provenance...``, which landed on main first;
same renumbering-on-merge-order the 0012 docstring describes.)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oauth_states", sa.Column("return_to", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("oauth_states", "return_to")

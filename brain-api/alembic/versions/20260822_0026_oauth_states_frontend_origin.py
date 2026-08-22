"""Add frontend_origin to oauth_states for multi-origin OAuth callback redirects

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-22

A source-connector OAuth flow (Gmail/Drive/etc.) is entirely backend-driven —
the callback has no JWT and can't ask the frontend where it started, so the
final redirect has always used the single static ``FRONTEND_URL`` setting.
That breaks whenever more than one frontend origin legitimately points at the
same API (a local dev server alongside the deployed frontend): the browser
gets bounced to whichever one ``FRONTEND_URL`` happens to be, not the one the
flow actually began on.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oauth_states", sa.Column("frontend_origin", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("oauth_states", "frontend_origin")

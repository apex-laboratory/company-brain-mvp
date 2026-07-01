"""Add subdomain to oauth_states for subdomain-scoped OAuth providers

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-01

Subdomain-scoped providers (Zendesk, and later Jira) run their OAuth + API against
``{subdomain}.zendesk.com``. The subdomain is supplied by the connecting workspace at
the start of the flow and must survive until the callback, where it is handed to the
token exchange. It rides on the short-lived ``oauth_states`` row.

Nullable: only subdomain-scoped providers populate it; global-endpoint providers
(Notion, GitHub, Slack) leave it NULL. ``oauth_states`` has RLS enabled with no
policies (0008) and is reached only via the service-role connection, so no policy
change is needed for the new column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oauth_states", sa.Column("subdomain", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("oauth_states", "subdomain")

"""Stamp the originating connection on each source_event (Phase 3)

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-11

The extraction pipeline's context expanders need a valid access token for the
connection that produced an event. Without a connection reference on the row, the
expander fell back to "first connection for this (workspace, provider)", so a
second same-provider connection (two Slack workspaces, two GitHub orgs) expanded
every event with the wrong token — a silent 401 that degraded extraction to raw
single-message content.

``source_connection_id`` records which connection ingested the event so the token
is resolved exactly. Nullable + ON DELETE SET NULL: legacy rows and rows whose
connection was later removed keep working via the provider fallback.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_events",
        sa.Column(
            "source_connection_id",
            sa.Text(),
            sa.ForeignKey("source_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("source_events", "source_connection_id")

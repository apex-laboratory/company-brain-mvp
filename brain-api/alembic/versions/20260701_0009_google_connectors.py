"""Google Drive + Gmail connectors: opaque sync cursor, channel expiry, enum values

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-01

The Google connectors are the first whose incremental-sync cursor is an opaque token
(Drive changes ``pageToken``, Gmail ``historyId``) rather than a timestamp, and the
first to register expiring push channels. This migration adds:

- source_connections.sync_cursor (TEXT) — opaque per-connection cursor. The sync
  orchestrator stores the provider's next-cursor verbatim here; timestamp providers
  (Notion/GitHub) keep using last_synced_at and leave this NULL.
- webhook_subscriptions.expires_at (TIMESTAMPTZ) — Google watch channels expire in
  <= 7 days; the renewal cron re-watches rows nearing this.
- source_provider enum values 'google_drive' and 'gmail'. ``ALTER TYPE ... ADD VALUE``
  cannot run inside a transaction block, so it runs in an autocommit block.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_connections", sa.Column("sync_cursor", sa.Text, nullable=True))
    op.add_column(
        "webhook_subscriptions",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'google_drive'")
        op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'gmail'")


def downgrade() -> None:
    # Postgres cannot drop enum values; leave 'google_drive'/'gmail' in place.
    op.drop_column("webhook_subscriptions", "expires_at")
    op.drop_column("source_connections", "sync_cursor")

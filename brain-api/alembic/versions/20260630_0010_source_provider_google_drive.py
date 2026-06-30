"""Add 'google_drive' to the source_provider enum

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-30

Wires Google Drive into the read-only source connectors. Every table that stores
a connected source keys off the ``source_provider`` enum (source_connections,
source_channels, webhook_subscriptions, source_events, decisions, skills), so the
provider must exist at the type level before the connector can persist anything.

``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block, so it executes
in an ``autocommit_block``. ``IF NOT EXISTS`` keeps the migration idempotent.
Postgres has no ``DROP VALUE`` for enums, so the downgrade is a documented no-op —
the orphan label is harmless once no rows reference it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'google_drive'")


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type; nothing to undo.
    pass

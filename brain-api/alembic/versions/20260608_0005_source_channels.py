"""Source channels: replace monitored_ids JSONB blob with a proper table

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-08

Adds source_channels to replace the old source_connections.monitored_ids JSONB blob.
Each row is one channel/space/project the workspace has selected for ingestion.

- external_id: the provider-assigned id (channel id, space id, project key, etc.)
- name: human-readable label shown in the UI ('#cs-escalations', 'Policy Library')
- selected: whether this channel is currently active for ingestion
- item_count: cached count of items ingested from this channel

The unique constraint (source_id, external_id) prevents duplicates per connection.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_channels",
        sa.Column("id",           sa.Text, primary_key=True),              # chn_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("source_id",    sa.Text,
                                  sa.ForeignKey("source_connections.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("provider",     sa.Enum("slack", "notion", "github", "jira", "zendesk",
                                          name="source_provider", create_type=False),
                                  nullable=False),
        sa.Column("external_id",  sa.Text),                                # channel/space/project id at provider
        sa.Column("name",         sa.Text, nullable=False),                # '#cs-escalations'
        sa.Column("selected",     sa.Boolean,
                                  nullable=False, server_default=sa.text("FALSE")),
        sa.Column("item_count",   sa.Integer,
                                  nullable=False, server_default=sa.text("0")),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("source_id", "external_id",
                            name="source_channels_source_id_external_id_key"),
    )
    op.create_index(None, "source_channels", ["workspace_id", "source_id"])

    op.execute("ALTER TABLE source_channels ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("source_channels")

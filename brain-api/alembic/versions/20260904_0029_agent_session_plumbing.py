"""Phase-4 plumbing: the workspace vault's Brain credential, and a webhook lookup.

Two small changes, both forced by how phase 4 actually has to work.

**``agent_vaults.brain_credential_id``.** §5.4 keeps ``query_brain``'s own
credential in a per-workspace vault rather than in each user's, so it does not
eat one of their 20 credential slots. That credential needs a
find-or-create marker, and ``agent_credentials`` cannot hold it: that table's RLS
policy is ``user_id = current_user_id()``, and the workspace credential belongs to
nobody, so a row with a NULL ``user_id`` would be written and then be invisible to
every subsequent read — we would mint a fresh API key and a duplicate credential
on every single session create until the vault hit its 20-slot ceiling. Hanging
the pointer off the vault row instead puts it under ``agent_vaults``' policy,
which already contemplates a workspace-owned row (``user_id IS NULL OR user_id =
current_user_id()``).

It is a pointer, not a credential. The API key minted for it is stored the way
every other API key is — SHA-256 hash only, in ``api_keys`` — and the raw value
goes to Anthropic's vault and is dropped. Nothing here is a secret.

**An index on ``anthropic_session_id`` alone.** ``POST /webhooks/anthropic``
arrives with a session id and no tenant: Anthropic has no idea which of our
workspaces a session belongs to, and the delivery carries only ``{id, type}``.
The existing unique constraint is ``(workspace_id, anthropic_session_id)``, whose
leading column the webhook does not have, so the resolving lookup would seq-scan
every session in the table on every status change of every session. The id is
globally unique at Anthropic; the index is not declared unique here only because
that is a promise about a vendor's id space that we cannot enforce and do not
need.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The `cred_…` id Anthropic returned for this workspace's query_brain
    # credential. NULL until the first session that needs grounding provisions it.
    # NOT a secret: see the module docstring.
    op.add_column(
        "agent_vaults",
        sa.Column("brain_credential_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_agent_sessions_anthropic_id",
        "agent_sessions",
        ["anthropic_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_anthropic_id", table_name="agent_sessions")
    op.drop_column("agent_vaults", "brain_credential_id")

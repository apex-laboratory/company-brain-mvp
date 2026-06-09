"""Auth tables: refresh_tokens and oauth_states

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-08

Adds the two tables that support the self-issued JWT auth flow:

refresh_tokens — rotating refresh token store with family-based reuse detection.
  - token_hash: sha256 of the raw token; raw value lives only in the httpOnly cookie.
  - family_id: rotation family; if a revoked token is replayed, the whole family
    is revoked (token theft signal).
  - replaced_by: links the rotation chain for audit purposes.

oauth_states — short-lived signed CSRF state tokens for OAuth provider flows.
  - state_hash: hmac-sha256 of the state value sent to the provider.
  - consumed_at: single-use enforcement (mark consumed on callback).
  - Covers both sign-in OAuth (Google/GitHub/SAML) and source OAuth (Slack/Notion/…).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, UUID

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── refresh_tokens ────────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id",          UUID(as_uuid=True), primary_key=True,
                                 server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id",     sa.Text,
                                 sa.ForeignKey("users.id", ondelete="CASCADE"),
                                 nullable=False),
        sa.Column("token_hash",  sa.LargeBinary, nullable=False, unique=True),
        sa.Column("family_id",   sa.Text, nullable=False),
        sa.Column("expires_at",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at",  sa.DateTime(timezone=True)),
        sa.Column("replaced_by", UUID(as_uuid=True),
                                 sa.ForeignKey("refresh_tokens.id")),
        sa.Column("user_agent",  sa.Text),
        sa.Column("ip_address",  INET),
        sa.Column("created_at",  sa.DateTime(timezone=True),
                                 nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "refresh_tokens", ["user_id"])
    op.create_index(None, "refresh_tokens", ["family_id"])

    # ── oauth_states ──────────────────────────────────────────────────────────
    op.create_table(
        "oauth_states",
        sa.Column("id",            UUID(as_uuid=True), primary_key=True,
                                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id",       sa.Text,
                                   sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("workspace_id",  sa.Text,
                                   sa.ForeignKey("workspaces.id", ondelete="CASCADE")),
        sa.Column("provider",      sa.Text, nullable=False),               # google|github|saml|slack|…
        sa.Column("redirect_uri",  sa.Text, nullable=False),
        sa.Column("state_hash",    sa.LargeBinary, nullable=False, unique=True),
        sa.Column("expires_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at",   sa.DateTime(timezone=True)),
        sa.Column("created_at",    sa.DateTime(timezone=True),
                                   nullable=False, server_default=sa.text("now()")),
    )

    # RLS is not needed on these tables: lookups happen via privileged paths
    # (resolve by token_hash / state_hash before any workspace context exists).


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.drop_table("refresh_tokens")

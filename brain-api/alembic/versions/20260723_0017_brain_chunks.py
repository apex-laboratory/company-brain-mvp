"""brain_chunks: unified chat retrieval index (skill versions + evidence)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-23

The chat corpus for the "Ask the brain" surface (BRAIN_CHAT_RAG_PLAN Phases 2-3).
Kept separate from the operational ``skills`` table so the delivery/pipeline
surface is unchanged, while giving the brain a single embedded index to search:

  kind='skill_version' (P2) — current + historical skill bodies; is_current
                              distinguishes the live rule from superseded ones.
  kind='evidence'      (P3) — the source material that fed a skill (source_ref =
                              {provider, sourceItemId, url, label, author}).

Every chunk is skill-linked (FK CASCADE, so deleting a skill drops its chunks) and
workspace-isolated by RLS. ``chunk_key`` is the idempotency natural key: the
backfill upserts on (workspace_id, chunk_key), so re-runs skip already-indexed
chunks. System-written only (like source_events/agent_interactions): the FOR ALL
policy lets the tenant-scoped worker write within its workspace while RLS isolates
reads across tenants. brain_app gets DML via the provisioner's default privileges.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "brain_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),               # skill_version | evidence
        sa.Column("skill_id", sa.Text,
                  sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Text),                            # skill_version kind
        sa.Column("is_current", sa.Boolean),                      # true=live, false=superseded
        sa.Column("source_ref", JSONB),                           # evidence kind
        sa.Column("chunk_index", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("chunk_key", sa.Text, nullable=False),          # idempotency natural key
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "chunk_key",
                            name="brain_chunks_workspace_id_chunk_key_key"),
        sa.CheckConstraint("kind IN ('skill_version', 'evidence')",
                           name="brain_chunks_kind_check"),
    )
    op.create_index("ix_brain_chunks_workspace_id_skill_id", "brain_chunks",
                    ["workspace_id", "skill_id"])
    op.create_index("ix_brain_chunks_workspace_id_kind_is_current", "brain_chunks",
                    ["workspace_id", "kind", "is_current"])
    op.execute(
        "CREATE INDEX brain_chunks_embedding_hnsw ON brain_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # RLS: workspace isolation, system-written (mirrors source_events/agent_interactions).
    op.execute("ALTER TABLE brain_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE brain_chunks FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY brain_chunks_policy ON brain_chunks FOR ALL
          USING      (workspace_id = current_workspace_id())
          WITH CHECK (workspace_id = current_workspace_id())
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS brain_chunks_policy ON brain_chunks")
    op.drop_table("brain_chunks")

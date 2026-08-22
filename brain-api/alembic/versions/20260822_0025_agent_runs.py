"""agent_runs — trace ingestion for the self-improving loop (PRD Features 29-34)

The delivery layer becomes an ingestion surface: a harness pushes a successful
run's trace to ``POST /runs``, the success gate (Feature 30) decides whether it is
evidence of a *procedure*, and eligible runs cluster by task until the cluster is
worth one distillation.

Three column groups carry the design:

* ``trace`` is **evidence, not a product artifact**. It holds file contents, shell
  output and customer records, so it is redacted before insert and NULLed by the
  retention job once distillation completes. ``trace_digest`` is the durable
  half — step counts, tool histogram, cost, files touched — and outlives it. Any
  read path that needs "what did this run do" after retention must use the digest.
* ``eligible``/``ineligible_reason`` are NULL until the gate runs. NULL means
  "not yet judged", ``false`` means "judged and rejected" — they are different
  states and no query may collapse them, or ungated runs silently read as failures.
* ``cluster_id``/``distilled_at`` drive the cost control. Distillation fires per
  *cluster*, not per run (Feature 30's ``min_runs_per_cluster``), so an agent that
  runs one task 500 times costs one Sonnet call, not 500.

``task_embedding`` uses HNSW, not the ivfflat the PRD sketched: every other vector
column here (``skills``, ``brain_chunks``) is HNSW, and clustering reads the same
tuned cosine neighbourhood. Mixing index types would mean tuning two recall
profiles for one similarity threshold.

Ids are TEXT (``run_…``) like every other table, not the UUID in the PRD's SQL —
the house convention is prefixed ULIDs and ``skills.id`` is already TEXT.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Text, primary_key=True),                      # run_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        # Caller's own run id. Idempotency key for re-push: a harness that retries
        # a flush must not create a second row (see the unique constraint below).
        sa.Column("external_id", sa.Text),
        sa.Column("agent_name", sa.Text),
        sa.Column("task", sa.Text),
        sa.Column("task_embedding", Vector(1536)),
        # Caller-reported; the gate trusts but verifies against outcome_signals.
        sa.Column("outcome", sa.Text),
        sa.Column("outcome_signals", JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("eligible", sa.Boolean),                               # NULL until gated
        sa.Column("ineligible_reason", sa.Text),
        sa.Column("trace", JSONB),                                       # NULLed by retention
        sa.Column("trace_digest", JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("step_count", sa.Integer),
        sa.Column("tokens_used", sa.Integer),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("harness", sa.Text),
        sa.Column("ingest_mode", sa.Text, nullable=False,
                  server_default=sa.text("'live'")),
        sa.Column("sweep_id", sa.Text),
        sa.Column("cluster_id", sa.Text),
        sa.Column("skill_id", sa.Text, sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("distilled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        # Re-pushing the same external_id is an update, never a duplicate run.
        # Postgres allows many NULLs here, so harnesses without a run id of their
        # own still work — they just lose the idempotency guarantee.
        sa.UniqueConstraint("workspace_id", "external_id",
                            name="agent_runs_workspace_id_external_id_key"),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'partial', 'ambiguous', 'unknown')",
            name="agent_runs_outcome_check",
        ),
        sa.CheckConstraint(
            "ingest_mode IN ('live', 'backfill')", name="agent_runs_ingest_mode_check"
        ),
    )
    op.create_index(
        "ix_agent_runs_workspace_id_agent_name_outcome",
        "agent_runs", ["workspace_id", "agent_name", "outcome"],
    )
    # The clustering hot path: undistilled members of a cluster.
    op.create_index(
        "ix_agent_runs_cluster_pending", "agent_runs", ["workspace_id", "cluster_id"],
        postgresql_where=sa.text("distilled_at IS NULL"),
    )
    # The gate's work queue: ingested but not yet judged.
    op.create_index(
        "ix_agent_runs_ungated", "agent_runs", ["workspace_id", "created_at"],
        postgresql_where=sa.text("eligible IS NULL"),
    )
    op.execute(
        "CREATE INDEX agent_runs_task_embedding_hnsw ON agent_runs "
        "USING hnsw (task_embedding vector_cosine_ops)"
    )

    # RLS: workspace isolation, system-written (mirrors brain_chunks/agent_interactions).
    op.execute("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_runs_policy ON agent_runs FOR ALL
          USING      (workspace_id = current_workspace_id())
          WITH CHECK (workspace_id = current_workspace_id())
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_runs_policy ON agent_runs")
    op.drop_table("agent_runs")

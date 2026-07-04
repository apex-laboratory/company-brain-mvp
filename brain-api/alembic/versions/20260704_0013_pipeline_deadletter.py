"""Pipeline dead-letter bookkeeping + structured review payloads (Phase 3)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-04

The extraction pipeline (Phase 3) needs:

* ``source_events.attempts`` — count of full-pipeline runs for an event (each run
  already retries transient LLM failures internally); ``pipeline_meta`` — per-stage
  LLM costs, stage reached, and last error for the dead-letter path.
* A partial index on failed events so the retry-failed listing doesn't scan the
  whole audit trail.
* ``reviews.payload`` — structured card payload ({source_a, source_b,
  proposed_skill, matched_skill_id, boundary}); contradiction cards carry two full
  structured sources that the flat text columns can't hold.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("source_events", sa.Column("pipeline_meta", JSONB(), nullable=True))
    op.create_index(
        "ix_source_events_workspace_failed",
        "source_events",
        ["workspace_id"],
        postgresql_where=sa.text("outcome = 'failed'"),
    )
    op.add_column("reviews", sa.Column("payload", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("reviews", "payload")
    op.drop_index("ix_source_events_workspace_failed", table_name="source_events")
    op.drop_column("source_events", "pipeline_meta")
    op.drop_column("source_events", "attempts")

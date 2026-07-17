"""Pin the source_events.outcome vocabulary with a CHECK constraint (Phase 3)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-11

``outcome`` was a bare Text column, so a typo'd outcome string ('publish' vs
'published') was silently accepted by the DB, uncounted in sweep progress, and
unmatched by the ``outcome='failed'`` partial index. This CHECK pins the canonical
set (mirrors ``app.pipeline.types.ALL_OUTCOMES``); NULL stays allowed for freshly
inserted rows before the pipeline finalizes them.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OUTCOMES = (
    "queued", "published", "review", "draft",
    "discarded", "duplicate", "contradiction", "failed",
)


def upgrade() -> None:
    values = ", ".join(f"'{o}'" for o in _OUTCOMES)
    op.create_check_constraint(
        "source_events_outcome_check",
        "source_events",
        f"outcome IS NULL OR outcome IN ({values})",
    )


def downgrade() -> None:
    op.drop_constraint(
        "source_events_outcome_check", "source_events", type_="check"
    )

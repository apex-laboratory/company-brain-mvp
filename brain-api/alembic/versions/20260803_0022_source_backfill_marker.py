"""source_connections.backfilled_at — per-connection historical-backfill marker

Connecting a source ingests nothing: historical backfill happens only in a sweep
(``app/modules/sources/service.py`` step 5). Onboarding is skippable, so a source
connected later from the dashboard was never backfilled and relied entirely on
future webhooks. Fixing that needs a per-connection answer to "has this source's
history been imported?" — and no existing column answers it:

* ``sync_status`` is set to ``'healthy'`` by ``advance_sync`` after *any* successful
  sync, including a single partial chunk of Drive's chained backfill.
* ``last_synced_at`` is the incremental **cursor**, not a completion record.
* ``sweeps.progress`` is keyed by *provider*, not ``source_id``, and carries no
  record of which connection it actually covered.

So the marker has to be its own column. It is written by ``source_sync`` only when
a run finishes without a continuation cursor (``app/jobs/repository.py::advance_sync``),
which is why it correctly waits out Drive/Gmail's chained chunks, and it is read as
the ``needs_backfill`` predicate on ``GET /sources``.

Existing rows are stamped from ``last_synced_at`` rather than left NULL. A non-null
``last_synced_at`` proves a real ``source_sync`` ran — ``webhook_ingest`` inserts its
event without ever advancing the cursor — and a connection's *first* ``source_sync``
always covers the full ``lookback_days`` window, so those rows have been backfilled.
Leaving them NULL would make every already-imported source render "needs import".

Connections that only ever saw webhooks (Slack/GitHub connected post-onboarding —
the case this whole change exists for) keep ``last_synced_at IS NULL`` and so stay
unstamped, which is exactly right: their history has never been fetched.

No index. The predicate is only evaluated in ``list_connections``, which is already
a per-workspace sequential scan over a handful of rows.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_connections",
        sa.Column("backfilled_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE source_connections "
        "   SET backfilled_at = last_synced_at "
        " WHERE last_synced_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("source_connections", "backfilled_at")

"""Index source_events by connection + outcome for the per-source read report

The Sources page now answers "what did this source read, and how much of it
became knowledge?" — a rollup over ``source_events`` grouped by
``source_connection_id``, plus a per-stage discard breakdown for one connection.

``source_connection_id`` arrived in migration 0015 and has never been indexed.
The existing indexes are ``(workspace_id, provider, processed)`` and
``(workspace_id, sweep_id)``; neither helps a query grouping on the connection,
so both new queries would sequentially scan every event in the workspace. That
is invisible at today's row counts and becomes the Sources page's slowest query
as history accumulates — the rollup runs on *every* ``GET /sources``.

``outcome`` is the third column because both queries filter or aggregate on it
(``count(*) FILTER (WHERE outcome = …)``, and ``WHERE outcome = 'discarded'``
for the breakdown), so including it keeps the whole rollup index-only.

Index-only migration: no column, no data change, nothing to backfill.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_source_events_workspace_connection_outcome"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "source_events",
        ["workspace_id", "source_connection_id", "outcome"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="source_events")

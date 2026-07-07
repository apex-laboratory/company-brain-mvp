"""Make the source_connections unique constraint treat NULL account ids as equal

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-03

``UNIQUE(workspace_id, provider, external_account_id)`` used Postgres' default
NULLS-DISTINCT semantics, so two rows with a NULL ``external_account_id`` never
conflicted. Providers that can legitimately have no account id (e.g. Jira when the
grant has no accessible site) therefore inserted a *new* row on every reconnect —
duplicate connections that get double-synced and accumulate dead-token rows.

Recreating the constraint with ``NULLS NOT DISTINCT`` (Postgres 15+) makes a NULL
account id conflict with an existing NULL for the same (workspace, provider), so
``ON CONFLICT`` fires and the reconnect updates in place.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "source_connections_workspace_id_provider_account_key"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "source_connections", type_="unique")
    op.execute(
        f"ALTER TABLE source_connections "
        f"ADD CONSTRAINT {_CONSTRAINT} "
        f"UNIQUE NULLS NOT DISTINCT (workspace_id, provider, external_account_id)"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "source_connections", type_="unique")
    op.create_unique_constraint(
        _CONSTRAINT,
        "source_connections",
        ["workspace_id", "provider", "external_account_id"],
    )

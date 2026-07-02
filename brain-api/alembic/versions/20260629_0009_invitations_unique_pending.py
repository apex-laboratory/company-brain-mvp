"""Enforce one pending invitation per (workspace, email) at the DB level

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-29

The members invite flow pre-checks for an existing pending invitation before
inserting, but that check-then-insert is racy: two concurrent invites for the
same email both pass the check and both insert, because the only uniqueness on
``invitations`` is ``token_hash`` (always distinct). The service catches
``IntegrityError`` to map the race to a 409, but without a matching constraint
that handler can never fire.

This adds a **partial unique index** on ``(workspace_id, lower(email))`` limited
to ``status = 'pending'`` rows, so:

  - at most one *pending* invite can exist per workspace+email (race closed);
  - accepted / revoked / expired invites don't block re-inviting the same email
    later (they fall outside the partial predicate);
  - ``lower(email)`` matches the case-insensitive lookups the repository uses.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "invitations_workspace_email_pending_uq"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_INDEX_NAME}
        ON invitations (workspace_id, lower(email))
        WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")

"""brain_messages replay parity: trust + provenance + interaction_id.

A live answer carries trust ("skill"/"evidence"/"none"), a provenance dossier,
and the interaction id the flag-as-wrong button posts to — but none of that was
persisted, so a replayed thread (dashboard reload / history sidebar) silently
lost the trust badge, the provenance line, and the ability to flag an answer.

All three columns are nullable: user turns never have them, and historical
assistant turns predate them (the FE treats absent values as "don't render").

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("brain_messages", sa.Column("trust", sa.Text(), nullable=True))
    op.add_column("brain_messages", sa.Column("provenance", JSONB(), nullable=True))
    op.add_column("brain_messages", sa.Column("interaction_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("brain_messages", "interaction_id")
    op.drop_column("brain_messages", "provenance")
    op.drop_column("brain_messages", "trust")

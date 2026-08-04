"""workspaces.use_cases + use_case_other — the multi-select onboarding answer

The onboarding wizard's "why do you want to use Brainite?" question became a
multi-select (checkboxes) with a free-text "Other" option, so one column can no
longer hold the answer:

* ``primary_use_case`` is a single Text value and is what every existing read
  path uses, so it stays — it now holds the *first* selection.
* ``use_cases`` holds the whole set, as a jsonb array of the same ids.
* ``use_case_other`` holds the text typed behind the "Other" checkbox, kept out
  of the array so ``use_cases`` stays enum-pure and queryable.

Existing rows are backfilled to a one-element array from ``primary_use_case``
rather than left NULL: those workspaces did answer the question, and a NULL here
is indistinguishable from "answered nothing" for anyone reading the new column.

No index. ``use_cases`` is written at onboarding and read per-workspace by id;
nothing filters on it. jsonb (not text[]) matches the house convention for
list-valued columns (``sweeps.config``, ``brain_messages.sources``).

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("use_cases", postgresql.JSONB()))
    op.add_column("workspaces", sa.Column("use_case_other", sa.Text()))
    op.execute(
        "UPDATE workspaces "
        "   SET use_cases = jsonb_build_array(primary_use_case) "
        " WHERE primary_use_case IS NOT NULL AND use_cases IS NULL"
    )


def downgrade() -> None:
    op.drop_column("workspaces", "use_case_other")
    op.drop_column("workspaces", "use_cases")

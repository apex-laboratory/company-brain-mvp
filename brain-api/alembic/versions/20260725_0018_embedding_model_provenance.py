"""embedding_model provenance on skills + brain_chunks

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-25

Records **which** embedding model produced each stored vector.

Without this column a partial re-embed is undetectable. Cosine distance between
a vector from model A and one from model B is still a number in [0, 2] — pgvector
returns it happily — so a half-migrated table doesn't error, it silently returns
wrong neighbours. That failure mode is invisible to every existing test and to the
``SIMILARITY_THRESHOLD`` / ``_MATCH_THRESHOLD`` gates, which would keep comparing
scores from two incompatible vector spaces.

With it, "is this row's vector current?" is a column predicate, which is what the
re-embed jobs select on (``app/jobs/tasks/reembed_skills.py`` and the ``force``
path in ``brain_index_backfill``).

Existing rows are stamped ``text-embedding-3-small``: that is the
``settings.embedding_model`` default and it has never been overridden in any
deployed environment, so every vector currently in the database came from it.
Stamping them (rather than leaving NULL) keeps this migration from making the
whole corpus look stale and triggering a spurious full re-embed on first run.
Rows with no embedding stay NULL — the jobs treat NULL as "needs embedding",
which for those rows is correct.

No index is added. The staleness predicate compares against the *configured*
model, which isn't known at migration time, so no partial index can be written
for it; at MVP corpus size the sequential scan in a rarely-run maintenance job is
not worth an index that has to be rebuilt on every model change.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The model every pre-0018 vector was produced by (settings.embedding_model default).
_LEGACY_MODEL = "text-embedding-3-small"


def upgrade() -> None:
    op.add_column("skills", sa.Column("embedding_model", sa.Text))
    op.add_column("brain_chunks", sa.Column("embedding_model", sa.Text))

    for table in ("skills", "brain_chunks"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET embedding_model = :model "  # table name is a fixed literal
                f"WHERE embedding IS NOT NULL AND embedding_model IS NULL"
            ).bindparams(model=_LEGACY_MODEL)
        )


def downgrade() -> None:
    op.drop_column("brain_chunks", "embedding_model")
    op.drop_column("skills", "embedding_model")

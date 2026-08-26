"""Add agent_origin to api_keys: close the query-text path into agent_interactions

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-26

``query_brain`` escalates a miss into ``run_query_extraction`` and logs the raw
situation text into ``agent_interactions.query``. An agent that has just read a
customer's Slack DM will phrase its query using that content, so that column is
the one channel able to carry connector/file contents into the Brain verbatim:
the reviewed path is open, this unreviewed one must be closed.

``agent_origin`` marks a credential as non-dashboard. It defaults **true**, so
every API key (plugin, agent session, CI harness) is closed by default and the
open case has to be chosen deliberately; dashboard JWTs carry no row here and
stay ``False`` in ``AuthContext``. When set, ``query_brain`` skips extraction and
stores ``query = NULL`` while still recording the match, so usage counters, the
override flow and Feature 34 reinforcement are unaffected.

Cost of closing it today is zero: ``search_sources()`` returns ``[]`` until a
provider implements search, so extraction cannot currently mint a skill.

No backfill is needed for ``agent_interactions.query`` (already nullable) and no
data is rewritten: this only governs future writes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "agent_origin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "agent_origin")

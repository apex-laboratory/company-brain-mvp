"""Enable RLS on privileged public tables exposed via PostgREST

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-13

Supabase exposes the entire ``public`` schema through PostgREST using the public
anon key, so any table there without RLS is readable over the auto-generated REST
API (``/rest/v1/<table>``). Four tables were left without RLS by earlier migrations
because the backend reaches them only through privileged, pre-tenant code paths:

  - refresh_tokens   — rotating refresh-token hashes (sensitive)
  - oauth_states     — short-lived OAuth CSRF state (sensitive)
  - companies        — global company directory
  - alembic_version  — migration bookkeeping

That rationale held for the backend's own connection, but ignored the REST API
surface — the Supabase linter flags all four as ``rls_disabled_in_public`` (ERROR).

Fix: ENABLE (not FORCE) row level security with **no policies**. With ENABLE, the
table owner / service-role connection the backend uses still bypasses RLS, so the
privileged code paths and alembic itself keep working; the ``anon`` and
``authenticated`` PostgREST roles match no policy and get nothing. FORCE is wrong
here — it would subject the owner to the (absent) policies and break the backend's
pre-tenant access.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Privileged / global tables reached only via the backend's owner connection.
# Deny-all RLS closes the PostgREST surface without adding policies.
_PRIVILEGED_TABLES = ("refresh_tokens", "oauth_states", "companies", "alembic_version")


def upgrade() -> None:
    for table in _PRIVILEGED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in _PRIVILEGED_TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

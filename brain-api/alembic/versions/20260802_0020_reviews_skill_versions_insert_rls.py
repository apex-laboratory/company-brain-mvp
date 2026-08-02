"""Add INSERT policies for reviews and skill_versions

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-02

0007 gave ``reviews`` SELECT+UPDATE and ``skill_versions`` SELECT only, on the
assumption recorded in its comment — "System (backend) inserts reviews — no
member INSERT policy" — that those inserts would run on the privileged
(BYPASSRLS) connection. That is not how the app works: every write path opens
``get_tenant_session`` + ``run_in_tenant`` on the RESTRICTED ``brain_app`` role,
which IS subject to RLS. With RLS forced and no INSERT policy, Postgres rejects
the row outright:

    new row violates row-level security policy for table "reviews"

Nothing caught this because the only outcomes exercised so far were ``draft``
and ``discarded``, neither of which writes a review or a version row. Every
path above the review floor was broken:

* ``reviews``        — pipeline ``review`` outcome (confidence 0.70–0.90),
                       ``POST /skills`` (manual authoring), and the Feature 15a
                       override loop.
* ``skill_versions`` — pipeline ``published`` outcome (confidence >= 0.90) and
                       every review-approval path in ``reviews/service.py``.

Role gates differ between the two, and deliberately:

* ``reviews`` is workspace-scoped with NO role predicate. The override endpoint
  (``POST /interactions/{id}/override``) opens a review on behalf of an *agent*
  — an API key carrying role ``viewer`` — so an admin/editor gate would fail
  closed on a legitimate caller. Who may open a review is enforced at the API
  layer (``require_role`` / ``require_scope``); RLS here is the tenant-isolation
  backstop, not the authorization check.
* ``skill_versions`` mirrors ``skills_write`` (admin/editor). It is the version
  history of ``skills``, only ever written by the pipeline (which runs as
  ``admin``) and by the admin-only review endpoints, so it can carry the
  stricter predicate that its parent table already has.

Both are INSERT-only. Version rows are immutable history and reviews are
resolved through the existing ``reviews_update`` policy, so neither needs its
write surface widened further.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Workspace-scoped only — the override loop inserts as a `viewer` API key.
    op.execute("""
        CREATE POLICY reviews_insert ON reviews FOR INSERT
          WITH CHECK (workspace_id = current_workspace_id())
    """)
    # Mirrors skills_write: same actors, same authority as the parent row.
    op.execute("""
        CREATE POLICY skill_versions_insert ON skill_versions FOR INSERT
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND current_member_role() IN ('admin', 'editor')
          )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS skill_versions_insert ON skill_versions")
    op.execute("DROP POLICY IF EXISTS reviews_insert ON reviews")

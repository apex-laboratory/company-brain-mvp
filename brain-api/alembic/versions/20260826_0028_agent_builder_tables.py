"""Agent builder — six pointer tables (agent-builder-plan §5.1, phase 1)

User-built agents that reach their own tools at runtime over MCP, grounded in the
Brain. The runtime is Anthropic Managed Agents; **Brainite stores pointers, never
the content that flows through them.** Connector data goes MCP → Anthropic and
back and never transits our process, which is what buys the liability split.

That boundary is why three of these tables are defined as much by what they omit
as by what they hold:

* ``agent_credentials`` holds **no tokens.** An OAuth access/refresh pair transits
  the process once, at vault-create, is POSTed to Anthropic, and is never written
  here. This table records only *that* a user connected a provider, and the
  ``acr_`` id Anthropic gave back.
* ``agent_sessions`` holds **no transcript** — no message text, no tool arguments,
  no results. Only ``title`` (user-supplied) and ``stop_reason`` (an enum-ish
  token) are free text, and §5.5's schema test asserts exactly that. If a future
  reader wants "what did this session say", the answer is Anthropic's events API,
  proxied and never stored.
* ``agent_schedules`` holds **no run records.** Deployment runs, failures
  included, are read from Anthropic on demand.

**``agent_sessions`` is not ``agent_runs``** (agent-builder-plan §4.5). Three
``agent_*`` concepts now coexist and two of them will get confused:

  ``agent_interactions``  the episodic query log        (extraction / query_brain)
  ``agent_runs``          ingested coding-agent traces  (POST /runs, the plugin)
  ``agent_sessions``      agent-builder run pointers    (this migration)

``agent_runs`` deliberately *does* carry a trace, because it is evidence headed
for a success gate. ``agent_sessions`` deliberately does not, because it is a
pointer at somebody else's runtime. Adding a ``transcript`` column here would not
be an enhancement; it would move connector data onto our infrastructure and void
the split. The distinction lives in this comment because the next engineer reads
the migration, not the ORM filename.

**RLS.** ``agent_definitions`` carries the one policy with real thought in it:
private-by-default with explicit publish-to-workspace, so a member sees their own
drafts plus anything published. Every other table repeats ``workspace_id`` and
filters on it directly rather than joining to the parent — the same column-
predicate discipline the rest of the schema uses (``agent_runs``,
``brain_chunks``). A policy that has to subquery its parent is slower and is one
refactor away from being silently wrong; a column predicate cannot be.

The child tables intentionally do **not** re-check visibility. Reaching a
connector or session row requires knowing its parent's id, and every service path
loads the parent through ``agent_definitions`` first — where the visibility policy
does apply.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agent_definitions ─────────────────────────────────────────────────────
    op.create_table(
        "agent_definitions",
        sa.Column("id", sa.Text, primary_key=True),                      # agt_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.Text,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visibility", sa.Text, nullable=False,
                  server_default=sa.text("'private'")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("system_prompt", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("effort", sa.Text),
        # query_brain attached by default: the grounding is the product (§10).
        sa.Column("ground_in_brain", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        # Mirrors of Anthropic's objects. NULL until the first save syncs them --
        # agents are created lazily, so a draft exists here before it exists there.
        sa.Column("anthropic_agent_id", sa.Text),
        sa.Column("anthropic_agent_version", sa.Integer),
        # Per-session spend cap in minor units, copied onto every session and
        # deployment. NULL means "use the service default" -- never "unlimited".
        sa.Column("budget_cents", sa.Integer),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("visibility IN ('private', 'workspace')",
                           name="agent_definitions_visibility_check"),
        sa.CheckConstraint("status IN ('draft', 'active', 'archived')",
                           name="agent_definitions_status_check"),
    )
    op.create_index("ix_agent_definitions_workspace_owner", "agent_definitions",
                    ["workspace_id", "owner_user_id"])
    # The list query: everything visible to a caller, newest first.
    op.create_index("ix_agent_definitions_workspace_visibility", "agent_definitions",
                    ["workspace_id", "visibility", "created_at"])

    # ── agent_connectors ──────────────────────────────────────────────────────
    # No credential material. `name` is referenced by the agent's
    # mcp_toolset.mcp_server_name, so it is unique per agent, not per workspace.
    op.create_table(
        "agent_connectors",
        sa.Column("id", sa.Text, primary_key=True),                      # acn_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Text,
                  sa.ForeignKey("agent_definitions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("mcp_server_url", sa.Text, nullable=False),
        # NULL for a pasted custom URL; a catalog key otherwise.
        sa.Column("provider", sa.Text),
        sa.Column("tool_allowlist", JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("agent_id", "name", name="agent_connectors_agent_id_name_key"),
    )
    op.create_index("ix_agent_connectors_agent_id", "agent_connectors", ["agent_id"])

    # ── agent_vaults ──────────────────────────────────────────────────────────
    # One vault per (workspace, user). The workspace vault holding query_brain's
    # own credential is the row with user_id IS NULL -- see §5.4: attaching both
    # keeps query_brain from eating one of the user's 20 credential slots.
    op.create_table(
        "agent_vaults",
        sa.Column("id", sa.Text, primary_key=True),                      # avl_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("anthropic_vault_id", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    # Two partial uniques rather than one constraint: Postgres treats NULLs as
    # distinct, so a plain UNIQUE(workspace_id, user_id) would let a workspace
    # accumulate many "workspace vaults" -- exactly the row that must be single.
    op.create_index("uq_agent_vaults_user", "agent_vaults", ["workspace_id", "user_id"],
                    unique=True, postgresql_where=sa.text("user_id IS NOT NULL"))
    op.create_index("uq_agent_vaults_workspace", "agent_vaults", ["workspace_id"],
                    unique=True, postgresql_where=sa.text("user_id IS NULL"))

    # ── agent_credentials ─────────────────────────────────────────────────────
    # **The tokens are deliberately absent.** They transit once, at vault-create,
    # and are never written to disk. This row is the record that a connection
    # exists, so the UI can say "connected" and name what still needs authorizing.
    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.Text, primary_key=True),                      # acr_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Text,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("mcp_server_url", sa.Text, nullable=False),
        sa.Column("anthropic_credential_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text),
        sa.Column("connected_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        # A vault keys credentials uniquely by MCP server URL, so we cannot hold
        # two for one user and one server either.
        sa.UniqueConstraint("workspace_id", "user_id", "mcp_server_url",
                            name="agent_credentials_user_server_key"),
    )
    op.create_index("ix_agent_credentials_user", "agent_credentials",
                    ["workspace_id", "user_id"])

    # ── agent_sessions ────────────────────────────────────────────────────────
    # NO TRANSCRIPT. See the module docstring: `title` and `stop_reason` are the
    # only free-text columns, and a schema test enforces that. This is *not*
    # `agent_runs`, which carries a trace on purpose.
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Text, primary_key=True),                      # ass_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Text,
                  sa.ForeignKey("agent_definitions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Text,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("anthropic_session_id", sa.Text, nullable=False),
        # Pin the agent version this session ran against: an agent edited
        # mid-session must not retroactively change what a past run did.
        sa.Column("anthropic_agent_version", sa.Integer),
        sa.Column("title", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("stop_reason", sa.Text),
        sa.Column("list_cost_cents", sa.Integer),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "anthropic_session_id",
                            name="agent_sessions_anthropic_session_id_key"),
    )
    op.create_index("ix_agent_sessions_agent_started", "agent_sessions",
                    ["workspace_id", "agent_id", "started_at"])

    # ── agent_schedules ───────────────────────────────────────────────────────
    # user_id is whose vault fires it -- a schedule runs as a person, because the
    # connector credentials it needs live in that person's vault.
    op.create_table(
        "agent_schedules",
        sa.Column("id", sa.Text, primary_key=True),                      # asc_…
        sa.Column("workspace_id", sa.Text,
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Text,
                  sa.ForeignKey("agent_definitions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Text,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("anthropic_deployment_id", sa.Text),
        sa.Column("cron_expression", sa.Text, nullable=False),
        sa.Column("timezone", sa.Text, nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column("budget_cents", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active', 'paused')",
                           name="agent_schedules_status_check"),
    )
    op.create_index("ix_agent_schedules_agent_id", "agent_schedules",
                    ["workspace_id", "agent_id"])

    # ── RLS ───────────────────────────────────────────────────────────────────
    for table in (
        "agent_definitions", "agent_connectors", "agent_vaults",
        "agent_credentials", "agent_sessions", "agent_schedules",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # The one policy with real thought in it: own private agents plus anything
    # published to the workspace. WITH CHECK repeats the predicate so a member
    # cannot insert or re-own a row into somebody else's name.
    op.execute("""
        CREATE POLICY agent_definitions_policy ON agent_definitions FOR ALL
          USING (
            workspace_id = current_workspace_id()
            AND (visibility = 'workspace' OR owner_user_id = current_user_id())
          )
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND owner_user_id = current_user_id()
          )
    """)

    # Connectors and schedules are agent configuration: any member who can see
    # the agent can see how it is wired. Visibility is enforced when the parent
    # is loaded, so these stay column predicates.
    for table in ("agent_connectors", "agent_schedules"):
        op.execute(f"""
            CREATE POLICY {table}_policy ON {table} FOR ALL
              USING      (workspace_id = current_workspace_id())
              WITH CHECK (workspace_id = current_workspace_id())
        """)

    # Vaults, credentials and sessions are personal. A workspace vault
    # (user_id IS NULL) is readable by any member -- it holds query_brain's own
    # credential, not anybody's connector tokens.
    op.execute("""
        CREATE POLICY agent_vaults_policy ON agent_vaults FOR ALL
          USING (
            workspace_id = current_workspace_id()
            AND (user_id IS NULL OR user_id = current_user_id())
          )
          WITH CHECK (
            workspace_id = current_workspace_id()
            AND (user_id IS NULL OR user_id = current_user_id())
          )
    """)
    for table in ("agent_credentials", "agent_sessions"):
        op.execute(f"""
            CREATE POLICY {table}_policy ON {table} FOR ALL
              USING (
                workspace_id = current_workspace_id()
                AND user_id = current_user_id()
              )
              WITH CHECK (
                workspace_id = current_workspace_id()
                AND user_id = current_user_id()
              )
        """)


def downgrade() -> None:
    for table in (
        "agent_schedules", "agent_sessions", "agent_credentials",
        "agent_vaults", "agent_connectors", "agent_definitions",
    ):
        op.execute(f"DROP POLICY IF EXISTS {table}_policy ON {table}")
        op.drop_table(table)

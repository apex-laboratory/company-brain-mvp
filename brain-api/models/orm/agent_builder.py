"""Agent-builder pointer tables (migration 0028, agent-builder-plan §5.1).

Deliberately **not** in ``agent.py``: that module holds ``AgentInteraction``
(table ``agent_interactions``), the extraction pipeline's episodic query log,
which has nothing to do with this feature beyond sharing a prefix. Three
``agent_*`` concepts now coexist — ``agent_interactions`` (query log),
``agent_runs`` (ingested traces) and ``agent_sessions`` (below) — and the last
two are the pair that will get confused. See §4.5 and migration 0028's docstring.

These models exist for **alembic metadata only**; the runtime reads and writes
through ``app/modules/agents/repository.py`` in raw SQL, per
``BACKEND_BEST_PRACTICES.md``. They still have to be here: ``alembic/env.py``
sets ``target_metadata = Base.metadata`` with no ``include_object`` filter, so a
table with no model is a table that the next ``--autogenerate`` proposes to drop.

The omissions below are load-bearing, not oversights — no tokens on
``AgentCredential``, no transcript on ``AgentSession``, no run records on
``AgentSchedule``. Brainite stores pointers; the content stays on Anthropic's
side of the wall.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentDefinition(Base):
    """A user-built agent. Private to its owner until explicitly published."""
    __tablename__ = "agent_definitions"
    __table_args__ = (
        Index("ix_agent_definitions_workspace_owner", "workspace_id", "owner_user_id"),
        Index("ix_agent_definitions_workspace_visibility",
              "workspace_id", "visibility", "created_at"),
        CheckConstraint("visibility IN ('private', 'workspace')",
                        name="agent_definitions_visibility_check"),
        CheckConstraint("status IN ('draft', 'active', 'archived')",
                        name="agent_definitions_status_check"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # agt_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'private'")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)
    ground_in_brain: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # NULL until the first save syncs to Anthropic: a draft exists here first.
    anthropic_agent_id: Mapped[str | None] = mapped_column(Text)
    anthropic_agent_version: Mapped[int | None] = mapped_column(Integer)
    budget_cents: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentConnector(Base):
    """An MCP server this agent talks to. Holds no credential material."""
    __tablename__ = "agent_connectors"
    __table_args__ = (
        UniqueConstraint("agent_id", "name", name="agent_connectors_agent_id_name_key"),
        Index("ix_agent_connectors_agent_id", "agent_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # acn_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False
    )
    # Referenced by the agent's mcp_toolset.mcp_server_name, hence unique per agent.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_server_url: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)                   # NULL = custom URL
    tool_allowlist: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentVault(Base):
    """An Anthropic vault id. ``user_id IS NULL`` is the shared workspace vault.

    Two partial unique indexes rather than one constraint: Postgres treats NULLs
    as distinct, so a plain UNIQUE(workspace_id, user_id) would let a workspace
    accumulate many "workspace vaults" — the one row that must be singular.
    """
    __tablename__ = "agent_vaults"
    __table_args__ = (
        Index("uq_agent_vaults_user", "workspace_id", "user_id",
              unique=True, postgresql_where=text("user_id IS NOT NULL")),
        Index("uq_agent_vaults_workspace", "workspace_id",
              unique=True, postgresql_where=text("user_id IS NULL")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # avl_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE")
    )
    anthropic_vault_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The workspace vault's single query_brain credential (migration 0029). A
    # pointer, not a secret — the API key behind it is stored the way every other
    # one is, as a hash in ``api_keys``. NULL on a user vault, and on a workspace
    # vault until the first session that needs grounding provisions it.
    brain_credential_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentCredential(Base):
    """That a user connected a provider — **never the tokens**.

    The OAuth access/refresh pair transits the process once, at vault-create, is
    POSTed to Anthropic, and is never written to Postgres. Adding a token column
    here would move the credential onto our infrastructure and void the
    liability split the whole design exists to buy.
    """
    __tablename__ = "agent_credentials"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "mcp_server_url",
                         name="agent_credentials_user_server_key"),
        Index("ix_agent_credentials_user", "workspace_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # acr_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_server_url: Mapped[str] = mapped_column(Text, nullable=False)
    anthropic_credential_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentSession(Base):
    """A pointer at one Anthropic session — **no transcript, ever**.

    ``title`` (user-supplied) and ``stop_reason`` (an enum-ish token) are the only
    free-text columns, and §5.5's schema test asserts exactly that. This is *not*
    ``agent_runs``: that table carries a trace on purpose, because it is evidence
    headed for a success gate. This one is a pointer at somebody else's runtime.
    """
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "anthropic_session_id",
                         name="agent_sessions_anthropic_session_id_key"),
        Index("ix_agent_sessions_agent_started", "workspace_id", "agent_id", "started_at"),
        # Migration 0029. The Anthropic webhook resolves a session by its vendor
        # id alone — it has no workspace to lead with, so the composite unique
        # above cannot serve it and the lookup would seq-scan every session.
        Index("ix_agent_sessions_anthropic_id", "anthropic_session_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # ass_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    anthropic_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Pinned so an agent edited mid-session cannot retroactively change what a
    # past run did.
    anthropic_agent_version: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    list_cost_cents: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSchedule(Base):
    """A cron deployment. Run records are read from Anthropic, never stored.

    ``user_id`` is whose vault fires it: a schedule runs as a person, because the
    connector credentials it needs live in that person's vault.
    """
    __tablename__ = "agent_schedules"
    __table_args__ = (
        Index("ix_agent_schedules_agent_id", "workspace_id", "agent_id"),
        CheckConstraint("status IN ('active', 'paused')",
                        name="agent_schedules_status_check"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # asc_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    anthropic_deployment_id: Mapped[str | None] = mapped_column(Text)
    cron_expression: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UTC'")
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    budget_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

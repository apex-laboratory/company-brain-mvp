"""Data access for the agent builder (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS
scopes each statement to the workspace *and* — on ``agent_definitions`` — to
what the caller may see: own private agents plus anything published (migration
0028). ``workspace_id`` is still bound explicitly on writes, as defence in
depth per the house rule.

**The visibility policy is doing real work here, so read the absences
carefully.** ``get`` has no ``owner_user_id`` predicate and ``list_visible`` has
no ``visibility`` predicate; both would be wrong to add, because RLS already
applies exactly the rule and a second copy in SQL is a second place to get it
wrong. What that means in practice: a row this caller cannot see does not come
back as a forbidden row, it comes back as **no row at all** — so the service
turns a missing row into 404, never 403. That is the right shape anyway: telling
someone "this agent exists but is not yours" leaks that it exists.

The methods below the ``privileged:`` headers are the documented exceptions to
the tenant rule: they run on the **privileged** pool and are the only ones here
that may be called outside ``run_in_tenant``. There are three groups, and each
one earns it by having no tenant to scope to yet:

* ``*_agent_oauth_state`` — the OAuth callback must resolve a state hash before
  any workspace context exists.
* ``resolve_session_tenant`` — an Anthropic webhook carries a session id and a
  type; the vendor has no idea which of our workspaces it belongs to. This
  answers exactly that and returns nothing else, so the tenant-scoped write can
  then happen normally.
* ``insert_brain_api_key`` — ``api_keys`` is admin-only at RLS, but the caller
  who first needs Brain grounding is routinely a viewer. Read its docstring: the
  safety here is that no part of the row is caller-controlled.

Nothing here calls Anthropic. The vendor round-trip happens in the service,
outside the transaction (``BACKEND_BEST_PRACTICES.md`` §7); the columns below
are only ever the *mirror* of what that call returned.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Selected by every read so the row → response mapping has one shape. Ordered to
# match the response schema, not the table, so a diff between them is readable.
_COLUMNS = """
    id, workspace_id, owner_user_id, name, description, system_prompt,
    model, effort, ground_in_brain, budget_cents, visibility, status,
    anthropic_agent_id, anthropic_agent_version, created_at, updated_at
"""

# Every column ``agent_credentials`` has. Worth reading as an assertion rather
# than a list: there is no token here, and adding one would move a user's
# connector secret onto our infrastructure (migration 0028).
_CREDENTIAL_COLUMNS = """
    id, workspace_id, user_id, provider, mcp_server_url,
    anthropic_credential_id, display_name, connected_at
"""

# Every column ``agent_sessions`` has, and worth reading as an assertion too:
# there is no transcript here, no message text, no tool arguments. ``title`` and
# ``stop_reason`` are the only free-text columns and §5.5's schema test says so.
_SESSION_COLUMNS = """
    id, workspace_id, agent_id, user_id, anthropic_session_id,
    anthropic_agent_version, title, status, stop_reason,
    list_cost_cents, input_tokens, output_tokens, started_at, ended_at
"""


# Namespace for this module's rows in the shared ``oauth_states`` table. See
# ``create_agent_oauth_state`` for why an un-namespaced provider would be a hole
# rather than a nuisance.
_STATE_NAMESPACE = "agent:"


def _state_provider(provider: str) -> str:
    return f"{_STATE_NAMESPACE}{provider}"


class AgentsRepository:
    async def list_visible(
        self, session: AsyncSession, *, workspace_id: str
    ) -> list[dict[str, Any]]:
        """Every agent the caller may see, newest first.

        No ``visibility`` filter and no ``owner_user_id`` filter: RLS is the
        filter. Archived agents are excluded because an archive on this API is
        irreversible and terminal — leaving them in the list would offer the user
        a card whose every action is dead.
        """
        rows = await session.execute(
            text(
                f"""
                SELECT {_COLUMNS}
                  FROM agent_definitions
                 WHERE workspace_id = :workspace_id
                   AND status <> 'archived'
                 ORDER BY created_at DESC
                """
            ),
            {"workspace_id": workspace_id},
        )
        return [dict(row) for row in rows.mappings()]

    async def get(
        self, session: AsyncSession, *, workspace_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        """One agent, or ``None`` when it does not exist *or* is not visible.

        The two cases are deliberately indistinguishable here — see the module
        docstring on why the service must render both as 404.
        """
        row = await session.execute(
            text(
                f"""
                SELECT {_COLUMNS}
                  FROM agent_definitions
                 WHERE id = :agent_id AND workspace_id = :workspace_id
                """
            ),
            {"agent_id": agent_id, "workspace_id": workspace_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def insert(
        self,
        session: AsyncSession,
        *,
        agent_id: str,
        workspace_id: str,
        owner_user_id: str,
        name: str,
        description: str | None,
        system_prompt: str | None,
        model: str | None,
        effort: str | None,
        ground_in_brain: bool,
        budget_cents: int | None,
        anthropic_agent_id: str | None,
        anthropic_agent_version: int | None,
    ) -> dict[str, Any]:
        """Create one agent, owned by the caller. Returns the stored row.

        ``owner_user_id`` is bound from the auth context, never from the request
        body: the RLS ``WITH CHECK`` requires it to equal ``current_user_id()``,
        so a body-supplied owner would fail the policy rather than silently
        create someone else's agent — but relying on that as the *only* guard
        would make a body field that looks assignable and is not.
        """
        row = await session.execute(
            text(
                f"""
                INSERT INTO agent_definitions (
                    id, workspace_id, owner_user_id, name, description,
                    system_prompt, model, effort, ground_in_brain, budget_cents,
                    anthropic_agent_id, anthropic_agent_version
                ) VALUES (
                    :id, :workspace_id, :owner_user_id, :name, :description,
                    :system_prompt, :model, :effort, :ground_in_brain, :budget_cents,
                    :anthropic_agent_id, :anthropic_agent_version
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                "id": agent_id,
                "workspace_id": workspace_id,
                "owner_user_id": owner_user_id,
                "name": name,
                "description": description,
                "system_prompt": system_prompt,
                "model": model,
                "effort": effort,
                "ground_in_brain": ground_in_brain,
                "budget_cents": budget_cents,
                "anthropic_agent_id": anthropic_agent_id,
                "anthropic_agent_version": anthropic_agent_version,
            },
        )
        return dict(row.mappings().one())

    async def update(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        agent_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Apply a partial update. Returns the new row, or ``None`` if invisible.

        ``fields`` carries **only the keys the caller actually sent** — the
        service builds it from ``exclude_unset``, so a ``NULL`` in here is an
        intentional clear rather than an unmentioned field. The column list is
        assembled from a fixed allowlist and each value is bound, so no caller
        input reaches the SQL text.
        """
        allowed = {
            "name", "description", "system_prompt", "model", "effort",
            "ground_in_brain", "budget_cents", "visibility", "status",
            "anthropic_agent_id", "anthropic_agent_version",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            # Nothing to write, but the caller still wants the current row —
            # and an empty SET list is a syntax error, not a no-op.
            return await self.get(
                session, workspace_id=workspace_id, agent_id=agent_id
            )

        assignments = ", ".join(f"{column} = :{column}" for column in updates)
        row = await session.execute(
            text(
                f"""
                UPDATE agent_definitions
                   SET {assignments}, updated_at = now()
                 WHERE id = :agent_id AND workspace_id = :workspace_id
                RETURNING {_COLUMNS}
                """
            ),
            {**updates, "agent_id": agent_id, "workspace_id": workspace_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def set_visibility(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        agent_id: str,
        visibility: str,
    ) -> dict[str, Any] | None:
        """Publish to the workspace, or take it back private.

        Separate from ``update`` because it is the one field a non-owner must
        never change, and the RLS ``WITH CHECK`` on ``agent_definitions`` is what
        stops them: it requires ``owner_user_id = current_user_id()`` on write,
        so a member who can *read* a published agent still cannot unpublish it —
        the UPDATE matches no row and this returns ``None``.
        """
        row = await session.execute(
            text(
                f"""
                UPDATE agent_definitions
                   SET visibility = :visibility, updated_at = now()
                 WHERE id = :agent_id AND workspace_id = :workspace_id
                RETURNING {_COLUMNS}
                """
            ),
            {
                "visibility": visibility,
                "agent_id": agent_id,
                "workspace_id": workspace_id,
            },
        )
        found = row.mappings().first()
        return dict(found) if found else None

    # ── connectors ────────────────────────────────────────────────────────────

    async def list_connectors(
        self, session: AsyncSession, *, workspace_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        """This agent's MCP servers, oldest first.

        Order is stable and meaningful: the service turns this list into
        Anthropic's ``mcp_servers`` and ``tools`` arrays, which are replaced
        wholesale on every write. A non-deterministic order would mint a new
        agent version on saves that changed nothing.
        """
        rows = await session.execute(
            text(
                """
                SELECT id, workspace_id, agent_id, name, mcp_server_url,
                       provider, tool_allowlist, created_at
                  FROM agent_connectors
                 WHERE agent_id = :agent_id AND workspace_id = :workspace_id
                 ORDER BY created_at, id
                """
            ),
            {"agent_id": agent_id, "workspace_id": workspace_id},
        )
        return [dict(row) for row in rows.mappings()]

    async def insert_connector(
        self,
        session: AsyncSession,
        *,
        connector_id: str,
        workspace_id: str,
        agent_id: str,
        name: str,
        mcp_server_url: str,
        provider: str | None,
        tool_allowlist: list[str],
    ) -> dict[str, Any]:
        """Declare one MCP server. Raises ``IntegrityError`` on a duplicate name.

        The ``(agent_id, name)`` unique constraint is load-bearing rather than
        tidy: ``name`` is what ``mcp_toolset.mcp_server_name`` resolves against,
        so two connectors sharing one would make the agent's tool config
        ambiguous. The service catches the violation and renders it as a 409.
        """
        row = await session.execute(
            text(
                """
                INSERT INTO agent_connectors (
                    id, workspace_id, agent_id, name, mcp_server_url,
                    provider, tool_allowlist
                ) VALUES (
                    :id, :workspace_id, :agent_id, :name, :mcp_server_url,
                    :provider, CAST(:tool_allowlist AS jsonb)
                )
                RETURNING id, workspace_id, agent_id, name, mcp_server_url,
                          provider, tool_allowlist, created_at
                """
            ),
            {
                "id": connector_id,
                "workspace_id": workspace_id,
                "agent_id": agent_id,
                "name": name,
                "mcp_server_url": mcp_server_url,
                "provider": provider,
                "tool_allowlist": json.dumps(tool_allowlist),
            },
        )
        return dict(row.mappings().one())

    async def delete_connector(
        self, session: AsyncSession, *, workspace_id: str, agent_id: str, connector_id: str
    ) -> bool:
        """Remove one MCP server. ``False`` when it was not there to remove.

        Scoped by ``agent_id`` as well as id, so a connector id from another
        agent cannot be deleted through this agent's path even inside one
        workspace.
        """
        result = await session.execute(
            text(
                """
                DELETE FROM agent_connectors
                 WHERE id = :connector_id
                   AND agent_id = :agent_id
                   AND workspace_id = :workspace_id
                """
            ),
            {
                "connector_id": connector_id,
                "agent_id": agent_id,
                "workspace_id": workspace_id,
            },
        )
        return bool(result.rowcount)

    # ── vaults ────────────────────────────────────────────────────────────────
    #
    # Two shapes share this table, told apart by ``user_id`` (§5.4). A row with a
    # user is that person's connector vault; the single row with ``user_id IS
    # NULL`` is the workspace vault holding ``query_brain``'s own credential. Two
    # partial unique indexes keep each singular — Postgres treats NULLs as
    # distinct, so one plain UNIQUE would let workspace vaults accumulate.

    async def get_vault(
        self, session: AsyncSession, *, workspace_id: str, user_id: str | None
    ) -> dict[str, Any] | None:
        """This user's vault, or the workspace vault when ``user_id`` is ``None``.

        ``IS NOT DISTINCT FROM`` rather than ``=``: the workspace vault is keyed
        by a NULL, and ``user_id = NULL`` is never true, so the ordinary
        comparison would silently never find it and we would mint a new vault on
        every session create until the partial unique index started rejecting them.
        """
        row = await session.execute(
            text(
                """
                SELECT id, workspace_id, user_id, anthropic_vault_id,
                       brain_credential_id, created_at
                  FROM agent_vaults
                 WHERE workspace_id = :workspace_id
                   AND user_id IS NOT DISTINCT FROM :user_id
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def insert_vault(
        self,
        session: AsyncSession,
        *,
        vault_id: str,
        workspace_id: str,
        user_id: str | None,
        anthropic_vault_id: str,
    ) -> dict[str, Any] | None:
        """Record a vault we just created at Anthropic.

        ``ON CONFLICT DO NOTHING`` returning ``None`` is the concurrent-connect
        case: two OAuth callbacks for the same user raced, both created a vault at
        Anthropic, and only one row can survive. The caller re-reads and uses the
        winner. The loser's remote vault is orphaned and logged — deleting it
        would risk deleting the winner's on a mis-read, and an empty vault costs
        nothing.
        """
        row = await session.execute(
            text(
                """
                INSERT INTO agent_vaults (id, workspace_id, user_id, anthropic_vault_id)
                VALUES (:id, :workspace_id, :user_id, :anthropic_vault_id)
                ON CONFLICT DO NOTHING
                RETURNING id, workspace_id, user_id, anthropic_vault_id,
                          brain_credential_id, created_at
                """
            ),
            {
                "id": vault_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "anthropic_vault_id": anthropic_vault_id,
            },
        )
        found = row.mappings().first()
        return dict(found) if found else None

    # ── credentials ───────────────────────────────────────────────────────────
    #
    # **No token columns, and none may be added** (migration 0028). These rows say
    # *that* a user connected a provider and carry the id Anthropic gave back; the
    # secret itself transits the process once and is never written.

    async def list_credentials(
        self, session: AsyncSession, *, workspace_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Everything this user has connected, newest first."""
        rows = await session.execute(
            text(
                f"""
                SELECT {_CREDENTIAL_COLUMNS}
                  FROM agent_credentials
                 WHERE workspace_id = :workspace_id AND user_id = :user_id
                 ORDER BY connected_at DESC
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        return [dict(row) for row in rows.mappings()]

    async def get_credential(
        self, session: AsyncSession, *, workspace_id: str, user_id: str, credential_id: str
    ) -> dict[str, Any] | None:
        row = await session.execute(
            text(
                f"""
                SELECT {_CREDENTIAL_COLUMNS}
                  FROM agent_credentials
                 WHERE id = :credential_id
                   AND workspace_id = :workspace_id
                   AND user_id = :user_id
                """
            ),
            {
                "credential_id": credential_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def get_credential_for_server(
        self, session: AsyncSession, *, workspace_id: str, user_id: str, mcp_server_url: str
    ) -> dict[str, Any] | None:
        """The existing connection for one MCP server, if any.

        Keyed on the URL rather than the provider because that is how a vault
        keys its credentials: two catalog entries pointing at one server are one
        credential, and re-connecting either must replace the same slot.
        """
        row = await session.execute(
            text(
                f"""
                SELECT {_CREDENTIAL_COLUMNS}
                  FROM agent_credentials
                 WHERE workspace_id = :workspace_id
                   AND user_id = :user_id
                   AND mcp_server_url = :mcp_server_url
                """
            ),
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "mcp_server_url": mcp_server_url,
            },
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def upsert_credential(
        self,
        session: AsyncSession,
        *,
        credential_id: str,
        workspace_id: str,
        user_id: str,
        provider: str,
        mcp_server_url: str,
        anthropic_credential_id: str,
        display_name: str | None,
    ) -> dict[str, Any]:
        """Record a connection, replacing this user's previous one for the server.

        The upsert target is ``(workspace_id, user_id, mcp_server_url)`` — the
        vault's own uniqueness rule, mirrored — so a re-connect updates the row in
        place and keeps its id stable for anything holding it. ``connected_at`` is
        refreshed because it means "when this token was minted", which is what the
        UI is actually reporting.
        """
        row = await session.execute(
            text(
                f"""
                INSERT INTO agent_credentials (
                    id, workspace_id, user_id, provider, mcp_server_url,
                    anthropic_credential_id, display_name
                ) VALUES (
                    :id, :workspace_id, :user_id, :provider, :mcp_server_url,
                    :anthropic_credential_id, :display_name
                )
                ON CONFLICT ON CONSTRAINT agent_credentials_user_server_key
                DO UPDATE SET provider = EXCLUDED.provider,
                              anthropic_credential_id = EXCLUDED.anthropic_credential_id,
                              display_name = EXCLUDED.display_name,
                              connected_at = now()
                RETURNING {_CREDENTIAL_COLUMNS}
                """
            ),
            {
                "id": credential_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "provider": provider,
                "mcp_server_url": mcp_server_url,
                "anthropic_credential_id": anthropic_credential_id,
                "display_name": display_name,
            },
        )
        return dict(row.mappings().one())

    async def delete_credential_row(
        self, session: AsyncSession, *, workspace_id: str, user_id: str, credential_id: str
    ) -> bool:
        """Forget one connection. ``False`` when it was not there to forget."""
        result = await session.execute(
            text(
                """
                DELETE FROM agent_credentials
                 WHERE id = :credential_id
                   AND workspace_id = :workspace_id
                   AND user_id = :user_id
                """
            ),
            {
                "credential_id": credential_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
        )
        return bool(result.rowcount)

    async def list_unauthorized_connectors(
        self, session: AsyncSession, *, workspace_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Connectors on agents this caller can see that they have no credential for.

        This is the "Needs your GitHub account" list, and it is a SQL question
        rather than a client-side set difference for one reason: the join to
        ``agent_definitions`` is what applies the visibility policy. Computing it
        in Python from two separate lists would need the connector list of every
        agent in the workspace — including the private ones RLS is hiding.

        Matched on ``mcp_server_url``, not ``provider``: a custom pasted URL has no
        provider at all, and it still needs a credential.
        """
        rows = await session.execute(
            text(
                """
                SELECT c.id            AS connector_id,
                       c.name          AS connector_name,
                       c.provider      AS provider,
                       c.mcp_server_url AS mcp_server_url,
                       d.id            AS agent_id,
                       d.name          AS agent_name
                  FROM agent_connectors c
                  JOIN agent_definitions d
                    ON d.id = c.agent_id
                   AND d.workspace_id = c.workspace_id
                 WHERE c.workspace_id = :workspace_id
                   AND d.status <> 'archived'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM agent_credentials cr
                        WHERE cr.workspace_id = c.workspace_id
                          AND cr.user_id = :user_id
                          AND cr.mcp_server_url = c.mcp_server_url
                   )
                 ORDER BY d.name, c.name
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        return [dict(row) for row in rows.mappings()]

    # ── sessions ──────────────────────────────────────────────────────────────
    #
    # **No transcript column exists and none may be added** (migration 0028). Every
    # method below writes pointers and counters: the vendor's session id, a status
    # token, two token counts. "What did this session say" is answered by proxying
    # Anthropic's events API, never by reading Postgres.
    #
    # ``agent_sessions``' RLS policy is ``workspace_id = current_workspace_id() AND
    # user_id = current_user_id()`` — personal, not workspace-wide. So a member
    # cannot see another member's runs of the same published agent, and the reads
    # below need no ``user_id`` predicate of their own.

    async def insert_session(
        self,
        session: AsyncSession,
        *,
        session_row_id: str,
        workspace_id: str,
        agent_id: str,
        user_id: str,
        anthropic_session_id: str,
        anthropic_agent_version: int | None,
        title: str | None,
        status: str | None,
    ) -> dict[str, Any]:
        row = await session.execute(
            text(
                f"""
                INSERT INTO agent_sessions
                    (id, workspace_id, agent_id, user_id, anthropic_session_id,
                     anthropic_agent_version, title, status)
                VALUES (:id, :workspace_id, :agent_id, :user_id, :anthropic_session_id,
                        :anthropic_agent_version, :title, :status)
                RETURNING {_SESSION_COLUMNS}
                """
            ),
            {
                "id": session_row_id,
                "workspace_id": workspace_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "anthropic_session_id": anthropic_session_id,
                "anthropic_agent_version": anthropic_agent_version,
                "title": title,
                "status": status,
            },
        )
        return dict(row.mappings().one())

    async def get_session_row(
        self, session: AsyncSession, *, workspace_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """One session of the caller's. Invisible and nonexistent are both ``None``."""
        row = await session.execute(
            text(
                f"""
                SELECT {_SESSION_COLUMNS}
                  FROM agent_sessions
                 WHERE workspace_id = :workspace_id AND id = :session_id
                """
            ),
            {"workspace_id": workspace_id, "session_id": session_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def list_sessions(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The caller's sessions, newest first, optionally for one agent."""
        rows = await session.execute(
            text(
                f"""
                SELECT {_SESSION_COLUMNS}
                  FROM agent_sessions
                 WHERE workspace_id = :workspace_id
                   AND (:agent_id::text IS NULL OR agent_id = :agent_id)
                 ORDER BY started_at DESC
                 LIMIT :limit
                """
            ),
            {"workspace_id": workspace_id, "agent_id": agent_id, "limit": limit},
        )
        return [dict(row) for row in rows.mappings().all()]

    async def count_live_sessions(
        self, session: AsyncSession, *, workspace_id: str
    ) -> int:
        """How many of the caller's sessions are still costing money.

        ``ended_at IS NULL`` rather than a status test: status is a mirror that
        only advances when a webhook lands, so a workspace whose webhook is
        misconfigured would show every session as forever ``running`` under a
        status predicate — and the cap would lock the user out permanently. The
        terminal write sets both, so the timestamp is the one that means "we know
        this is over".
        """
        row = await session.execute(
            text(
                """
                SELECT count(*) AS live
                  FROM agent_sessions
                 WHERE workspace_id = :workspace_id AND ended_at IS NULL
                """
            ),
            {"workspace_id": workspace_id},
        )
        return int(row.scalar_one())

    async def update_session_state(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        session_id: str,
        status: str | None = None,
        stop_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        ended: bool = False,
    ) -> dict[str, Any] | None:
        """Fold a vendor-reported state change into the mirror.

        Every field is COALESCE'd against the stored value, so a webhook that
        reports a status but no usage cannot blank the counters a previous
        delivery set. ``ended`` is a flag rather than a timestamp parameter
        because the vendor's terminal event carries no end time we could trust,
        and ``now()`` on a monotonic clock we control is the honest answer.
        """
        row = await session.execute(
            text(
                f"""
                UPDATE agent_sessions
                   SET status        = COALESCE(:status, status),
                       stop_reason   = COALESCE(:stop_reason, stop_reason),
                       input_tokens  = COALESCE(:input_tokens, input_tokens),
                       output_tokens = COALESCE(:output_tokens, output_tokens),
                       ended_at      = CASE WHEN :ended THEN COALESCE(ended_at, now())
                                            ELSE ended_at END
                 WHERE workspace_id = :workspace_id AND id = :session_id
                RETURNING {_SESSION_COLUMNS}
                """
            ),
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "status": status,
                "stop_reason": stop_reason,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "ended": ended,
            },
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def set_vault_brain_credential(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        vault_row_id: str,
        credential_id: str,
    ) -> str | None:
        """Record the workspace vault's ``query_brain`` credential (migration 0029).

        ``WHERE brain_credential_id IS NULL`` makes this the race resolver: two
        first sessions starting together both create a credential at Anthropic,
        and only one write lands. The loser reads back the winner's id and
        abandons its own — the same shape as ``insert_vault``, and for the same
        reason. Returns the id that is now stored, whoever wrote it.
        """
        await session.execute(
            text(
                """
                UPDATE agent_vaults
                   SET brain_credential_id = :credential_id
                 WHERE workspace_id = :workspace_id
                   AND id = :vault_row_id
                   AND brain_credential_id IS NULL
                """
            ),
            {
                "workspace_id": workspace_id,
                "vault_row_id": vault_row_id,
                "credential_id": credential_id,
            },
        )
        row = await session.execute(
            text(
                """
                SELECT brain_credential_id
                  FROM agent_vaults
                 WHERE workspace_id = :workspace_id AND id = :vault_row_id
                """
            ),
            {"workspace_id": workspace_id, "vault_row_id": vault_row_id},
        )
        found = row.scalar_one_or_none()
        return str(found) if found else None

    # ── privileged: the tenantless webhook lookup ─────────────────────────────
    #
    # The second documented exception to the tenant rule, alongside the OAuth
    # states above. ``POST /webhooks/anthropic`` is a vendor callback: it carries
    # a session id and a type, no JWT, no API key, and no way to know which of our
    # workspaces the session belongs to. Something has to cross tenants once to
    # answer that, and this is it — a single lookup that returns only the ids
    # needed to open a properly-scoped transaction for the actual write.
    #
    # It reads three columns and no more. In particular it does not return the
    # session's own state, so a forged webhook that guessed a real vendor id
    # learns nothing from a successful call that it did not already supply.

    async def resolve_session_tenant(
        self, session: AsyncSession, *, anthropic_session_id: str
    ) -> dict[str, Any] | None:
        row = await session.execute(
            text(
                """
                SELECT id, workspace_id, user_id
                  FROM agent_sessions
                 WHERE anthropic_session_id = :anthropic_session_id
                """
            ),
            {"anthropic_session_id": anthropic_session_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None

    async def insert_brain_api_key(
        self,
        session: AsyncSession,
        *,
        key_id: str,
        workspace_id: str,
        name: str,
        key_hash: bytes,
        key_prefix: str,
        created_by: str,
    ) -> None:
        """Mint the workspace's ``query_brain`` key on the **privileged** pool.

        The third and last documented exception here, and the one that most wants
        justifying. ``api_keys``' RLS policy is admin-only, because a row there
        holds a usable credential's hash. But the caller who needs grounding is
        whoever starts the first session, and that is routinely a viewer — so
        going through the tenant pool would make an ordinary member's first run
        fail with an error only an admin could clear, on a resource they never
        asked for and cannot see.

        What keeps that safe is that nothing here is caller-controlled: the scope
        is fixed at ``brain:query``, ``workspace_id`` is bound from the resolved
        session context rather than from any request field, and the row is
        attributed to the user who caused it. The alternative — pre-provisioning
        at workspace creation — was rejected because it mints a live credential
        for every workspace that never builds an agent.
        """
        await session.execute(
            text(
                """
                INSERT INTO api_keys
                    (id, workspace_id, name, key_hash, key_prefix, scopes, created_by)
                VALUES (:id, :workspace_id, :name, :key_hash, :key_prefix,
                        ARRAY['brain:query'], :created_by)
                """
            ),
            {
                "id": key_id,
                "workspace_id": workspace_id,
                "name": name,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "created_by": created_by,
            },
        )
        await session.commit()

    # ── oauth_states (privileged pool, no RLS) ────────────────────────────────
    #
    # The three methods below break this class's one rule — they do NOT run in a
    # tenant transaction — because they cannot: the OAuth callback resolves a
    # state hash before any workspace context exists, which is why ``oauth_states``
    # has no RLS and is reached on the privileged pool (migration 0004, 0008).
    # ``app/modules/sources/repository.py`` holds a near-identical trio for the
    # ingestion flow. That duplication is deliberate: the two flows resolve to
    # different shapes, are namespaced apart on purpose (see ``provider`` below),
    # and folding them into one helper would make a change to either flow able to
    # break the other's consent path — which is the one path a user cannot retry
    # their way out of.

    async def create_agent_oauth_state(
        self,
        session: AsyncSession,
        *,
        state_hash: bytes,
        provider: str,
        redirect_uri: str,
        user_id: str,
        workspace_id: str,
        expires_at: datetime,
        return_to: str | None,
        frontend_origin: str | None,
    ) -> None:
        """Record a single-use state for an agent-credential consent.

        ``provider`` is stored **namespaced** as ``agent:{provider}``, and that is
        load-bearing rather than tidy. ``oauth_states`` is shared with the
        ingestion connectors, whose callback consumes by ``(state_hash,
        provider)``; an un-namespaced ``slack`` state minted here could be
        redeemed at ``/sources/slack/callback`` and would silently create a
        *source connection* — an agent credential turning into an ingestion
        pipeline is precisely the wall this feature exists to hold.
        """
        await session.execute(
            text(
                """
                INSERT INTO oauth_states
                    (user_id, workspace_id, provider, redirect_uri, state_hash,
                     expires_at, return_to, frontend_origin)
                VALUES (:user_id, :workspace_id, :provider, :redirect_uri, :state_hash,
                        :expires_at, :return_to, :frontend_origin)
                """
            ),
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "provider": _state_provider(provider),
                "redirect_uri": redirect_uri,
                "state_hash": state_hash,
                "expires_at": expires_at,
                "return_to": return_to,
                "frontend_origin": frontend_origin,
            },
        )
        await session.commit()

    async def consume_agent_oauth_state(
        self, session: AsyncSession, *, state_hash: bytes, provider: str, now: datetime
    ) -> dict[str, Any] | None:
        """Atomically claim an unconsumed, unexpired agent state for ``provider``.

        One ``UPDATE ... RETURNING`` so consumption is race-safe: a replayed state
        finds ``consumed_at`` already set and matches no row.
        """
        row = await session.execute(
            text(
                """
                UPDATE oauth_states
                   SET consumed_at = :now
                 WHERE state_hash = :state_hash
                   AND provider = :provider
                   AND consumed_at IS NULL
                   AND expires_at > :now
                RETURNING user_id, workspace_id, redirect_uri, return_to, frontend_origin
                """
            ),
            {"state_hash": state_hash, "provider": _state_provider(provider), "now": now},
        )
        found = row.mappings().first()
        await session.commit()
        return dict(found) if found else None

    async def peek_agent_oauth_state(
        self, session: AsyncSession, *, state_hash: bytes, provider: str, now: datetime
    ) -> dict[str, Any] | None:
        """Read a live state's redirect target without consuming it.

        Same predicate as the consume, read-only: the declined-consent leg needs
        somewhere to send the browser while leaving the single-use state
        retryable.
        """
        row = await session.execute(
            text(
                """
                SELECT return_to, frontend_origin
                  FROM oauth_states
                 WHERE state_hash = :state_hash
                   AND provider = :provider
                   AND consumed_at IS NULL
                   AND expires_at > :now
                """
            ),
            {"state_hash": state_hash, "provider": _state_provider(provider), "now": now},
        )
        found = row.mappings().first()
        return dict(found) if found else None

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

Nothing here calls Anthropic. The vendor round-trip happens in the service,
outside the transaction (``BACKEND_BEST_PRACTICES.md`` §7); the columns below
are only ever the *mirror* of what that call returned.
"""
from __future__ import annotations

import json
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

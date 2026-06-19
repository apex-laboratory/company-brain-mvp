"""Workspace settings data access — the only place this SQL lives (§2 layering).

Every query is workspace-scoped (``id = :workspace_id`` bound param) and runs
under the caller's tenant context (RLS backstop). All values are bound
parameters; the only interpolated SQL fragment is the SET clause in
:meth:`WorkspaceRepository.update_settings`, assembled from a fixed column
whitelist that never carries request data (§5 injection rule).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Columns the settings endpoint may update. The keys of the dict passed to
# ``update_settings`` are asserted to be a subset of this set, so the
# interpolated ``SET`` clause can only ever name these trusted constants.
UPDATABLE_COLUMNS: frozenset[str] = frozenset({"name", "domain"})


@dataclass(frozen=True)
class WorkspaceSettingsRow:
    id: str
    name: str
    slug: str
    domain: str | None
    plan: str
    seat_limit: int


@dataclass(frozen=True)
class UpdatedWorkspaceRow:
    id: str
    name: str
    domain: str | None


class WorkspaceRepository:
    """Stateless repository; methods take the session they run in."""

    async def get_settings(
        self, session: AsyncSession, workspace_id: str
    ) -> WorkspaceSettingsRow | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, name, slug, domain, plan, seat_limit
                    FROM workspaces
                    WHERE id = :workspace_id AND deleted_at IS NULL
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).first()
        if row is None:
            return None
        return WorkspaceSettingsRow(
            id=row.id,
            name=row.name,
            slug=row.slug,
            domain=row.domain,
            plan=str(row.plan),
            seat_limit=int(row.seat_limit),
        )

    async def get_identity(
        self, session: AsyncSession, workspace_id: str
    ) -> UpdatedWorkspaceRow | None:
        """The id/name/domain triple returned by an update (used for empty PATCH)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, name, domain
                    FROM workspaces
                    WHERE id = :workspace_id AND deleted_at IS NULL
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).first()
        if row is None:
            return None
        return UpdatedWorkspaceRow(id=row.id, name=row.name, domain=row.domain)

    async def update_settings(
        self, session: AsyncSession, workspace_id: str, fields: dict[str, str | None]
    ) -> UpdatedWorkspaceRow | None:
        """Apply ``fields`` to the workspace and return the updated identity.

        ``fields`` must be non-empty and its keys a subset of
        ``UPDATABLE_COLUMNS`` (guaranteed by the service). Returns ``None`` when
        no live workspace matches — mapped to 404 by the service. The caller
        commits the transaction.
        """
        if not fields:
            raise ValueError("update_settings requires at least one field")
        if not set(fields).issubset(UPDATABLE_COLUMNS):
            # Defensive: should be impossible given the validated request schema.
            raise ValueError(f"unupdatable columns: {set(fields) - UPDATABLE_COLUMNS}")

        # Column names come only from UPDATABLE_COLUMNS (trusted constants); all
        # values are bound parameters.
        assignments = ", ".join(f"{col} = :{col}" for col in fields)
        row = (
            await session.execute(
                text(
                    f"""
                    UPDATE workspaces
                    SET {assignments}, updated_at = now()
                    WHERE id = :workspace_id AND deleted_at IS NULL
                    RETURNING id, name, domain
                    """
                ).bindparams(workspace_id=workspace_id, **fields),
            )
        ).first()
        if row is None:
            return None
        return UpdatedWorkspaceRow(id=row.id, name=row.name, domain=row.domain)

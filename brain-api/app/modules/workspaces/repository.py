"""Workspace data access (the only place workspace SQL lives).

Workspace creation runs pre-tenant on the service-role session. All subsequent
tenant-scoped writes go through run_in_tenant before reaching these methods.
The settings update assembles its SET clause from a trusted column whitelist
to prevent injection (§5).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

UPDATABLE_COLUMNS: frozenset[str] = frozenset({"name", "domain"})


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    name: str
    slug: str
    plan: str


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
    """Stateless repository; all methods take the session they run in."""

    # ── creation ──────────────────────────────────────────────────────────────

    async def create_workspace(
        self,
        session: AsyncSession,
        *,
        id: str,
        name: str,
        slug: str,
        team_size: str,
        primary_use_case: str,
        created_by: str,
    ) -> WorkspaceRecord:
        """Insert a new workspace row and return it. Caller commits.

        Raises IntegrityError on a slug UNIQUE violation.
        """
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces
                        (id, name, slug, plan, seat_limit,
                         team_size, primary_use_case, created_by)
                    VALUES
                        (:id, :name, :slug, 'trial', 5,
                         :team_size, :primary_use_case, :created_by)
                    RETURNING id, name, slug, plan
                    """
                ).bindparams(
                    id=id,
                    name=name,
                    slug=slug,
                    team_size=team_size,
                    primary_use_case=primary_use_case,
                    created_by=created_by,
                )
            )
        ).one()
        return WorkspaceRecord(id=row.id, name=row.name, slug=row.slug, plan=row.plan)

    async def create_member(
        self,
        session: AsyncSession,
        *,
        id: str,
        workspace_id: str,
        user_id: str,
        role: str,
    ) -> None:
        """Insert a workspace_members row. Caller commits."""
        await session.execute(
            text(
                """
                INSERT INTO workspace_members (id, workspace_id, user_id, role)
                VALUES (:id, :workspace_id, :user_id, CAST(:role AS member_role))
                """
            ).bindparams(id=id, workspace_id=workspace_id, user_id=user_id, role=role)
        )

    async def update_onboarding(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        step: str,
        name: str | None,
        team_size: str | None,
        primary_use_case: str | None,
    ) -> None:
        """Partially update workspace onboarding fields. Caller commits."""
        sets = ["onboarding_step = :step", "updated_at = now()"]
        params: dict[str, object] = {"step": step, "workspace_id": workspace_id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if team_size is not None:
            sets.append("team_size = :team_size")
            params["team_size"] = team_size
        if primary_use_case is not None:
            sets.append("primary_use_case = :primary_use_case")
            params["primary_use_case"] = primary_use_case

        await session.execute(
            text(
                f"UPDATE workspaces SET {', '.join(sets)} WHERE id = :workspace_id"
            ).bindparams(**params)
        )

    # ── settings ──────────────────────────────────────────────────────────────

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
        """Apply fields to the workspace and return the updated identity."""
        if not fields:
            raise ValueError("update_settings requires at least one field")
        if not set(fields).issubset(UPDATABLE_COLUMNS):
            raise ValueError(f"unupdatable columns: {set(fields) - UPDATABLE_COLUMNS}")

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

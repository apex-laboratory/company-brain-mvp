"""Workspace business logic (BACKEND_BEST_PRACTICES.md §2 layering).

Covers workspace creation, onboarding progress saving, and workspace settings
(GET/PATCH). The service is framework-agnostic; the router passes request
metadata as plain values.
"""
from __future__ import annotations

import re
import secrets
import string
import unicodedata

from sqlalchemy.exc import IntegrityError

from app.config.database import get_session
from app.config.settings import settings
from app.modules.auth.tokens import mint_access_token
from app.modules.workspaces.repository import WorkspaceRepository
from app.modules.workspaces.schemas import (
    CreateWorkspaceOut,
    CreateWorkspaceRequest,
    OnboardingOut,
    OnboardingPatchRequest,
    SettingsResponse,
    UpdatedWorkspace,
    UpdateSettingsResponse,
    WorkspaceConfig,
    WorkspaceOut,
)
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.authorize import assert_workspace_member
from app.shared.middleware.with_tenant import run_in_tenant, tenant_session

_SUFFIX_CHARS = string.ascii_lowercase + string.digits

_NEXT_STEP: dict[str, str] = {
    "company": "connect",
    "connect": "configure",
    "configure": "build",
    "build": "done",
    "done": "done",
}


def _generate_slug(company_name: str) -> str:
    slug = unicodedata.normalize("NFKD", company_name)
    slug = slug.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
    return slug


def _slug_with_suffix(base_slug: str) -> str:
    suffix = "".join(secrets.choice(_SUFFIX_CHARS) for _ in range(4))
    return f"{base_slug}-{suffix}"


def _is_slug_conflict(exc: IntegrityError) -> bool:
    """True only when the IntegrityError is the workspaces.slug UNIQUE violation.

    Any other constraint (FK, PK, role cast) must not be mislabelled as a slug
    collision, so we don't retry it or report it as "slug already taken".
    """
    return "slug" in str(getattr(exc, "orig", exc)).lower()


def _brain_endpoint(slug: str) -> str:
    return f"https://{slug}.{settings.mcp_base_domain}/mcp"


class WorkspaceService:
    def __init__(self, repository: WorkspaceRepository | None = None) -> None:
        self._repository = repository or WorkspaceRepository()

    # ── workspace creation ─────────────────────────────────────────────────────

    async def create_workspace(
        self,
        request: CreateWorkspaceRequest,
        *,
        user_id: str,
        user_agent: str | None,
        ip: str | None,
    ) -> CreateWorkspaceOut:
        """Create a workspace, seed the admin member, and issue a scoped token."""
        base_slug = _generate_slug(request.company_name)
        workspace_id = generate_id("workspace")
        member_id = generate_id("member")

        # Try the clean slug first, then one suffixed variant on a slug
        # collision. A non-slug IntegrityError is re-raised as-is (an honest 500)
        # rather than mislabelled as a slug conflict.
        candidate_slugs = [base_slug, _slug_with_suffix(base_slug)]

        async with get_session() as session:
            workspace = None
            for attempt, slug in enumerate(candidate_slugs):
                try:
                    workspace = await self._repository.create_workspace(
                        session,
                        id=workspace_id,
                        name=request.company_name,
                        slug=slug,
                        team_size=request.team_size,
                        primary_use_case=request.primary_use_case,
                        created_by=user_id,
                    )
                    break
                except IntegrityError as exc:
                    await session.rollback()
                    if not _is_slug_conflict(exc):
                        raise
                    if attempt == len(candidate_slugs) - 1:
                        raise ConflictError(
                            "Workspace slug already taken. Please try a different "
                            "company name."
                        ) from exc

            assert workspace is not None  # loop breaks with a row or raises above

            await self._repository.create_member(
                session,
                id=member_id,
                workspace_id=workspace.id,
                user_id=user_id,
                role="admin",
            )
            await session.commit()

        access_token = mint_access_token(
            user_id=user_id,
            workspace_id=workspace.id,
            role="admin",
        )
        return CreateWorkspaceOut(
            workspace=WorkspaceOut(
                id=workspace.id,
                name=workspace.name,
                slug=workspace.slug,
                plan=workspace.plan,
            ),
            access_token=access_token,
        )

    # ── onboarding ────────────────────────────────────────────────────────────

    async def save_onboarding_step(
        self,
        workspace_id: str,
        user_id: str,
        role: str,
        request: OnboardingPatchRequest,
    ) -> OnboardingOut:
        """Persist onboarding progress and return the next step.

        Only workspace-level fields (step + company info) are written here.
        ``connected_providers``/``time_range``/``channels`` are accepted for the
        documented contract but persisted by the Source Integrations API, not by
        this endpoint.
        """
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, user_id, role):
                await self._repository.update_onboarding(
                    session,
                    workspace_id=workspace_id,
                    step=request.step,
                    name=request.company_name,
                    team_size=request.team_size,
                    primary_use_case=request.primary_use_case,
                )
                await session.commit()

        return OnboardingOut(
            status="saved",
            next_step=_NEXT_STEP[request.step],
        )

    # ── settings ──────────────────────────────────────────────────────────────

    async def get_settings(
        self, auth: AuthContext, workspace_id: str
    ) -> SettingsResponse:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            row = await self._repository.get_settings(session, workspace_id)

        if row is None:
            raise NotFoundError("Workspace")

        return SettingsResponse(
            workspace=WorkspaceConfig(
                name=row.name,
                domain=row.domain,
                plan=row.plan,
                seat_limit=row.seat_limit,
            ),
            brain_endpoint=_brain_endpoint(row.slug),
        )

    async def update_settings(
        self, auth: AuthContext, workspace_id: str, fields: dict[str, str | None]
    ) -> UpdateSettingsResponse:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            if fields:
                row = await self._repository.update_settings(
                    session, workspace_id, fields
                )
                await session.commit()
            else:
                row = await self._repository.get_identity(session, workspace_id)

        if row is None:
            raise NotFoundError("Workspace")

        return UpdateSettingsResponse(
            workspace=UpdatedWorkspace(id=row.id, name=row.name, domain=row.domain)
        )

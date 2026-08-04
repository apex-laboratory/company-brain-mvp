"""Tests for POST /workspaces (KAN-51).

Two layers:
  * Service unit tests — drive WorkspaceService against a fake repository and
    fake DB session (no Postgres), asserting business rules and token claims.
  * Router contract tests — drive the FastAPI app through an ASGI transport
    with the service stubbed, asserting the response envelope, status codes,
    camelCase fields, and validation mapping.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from app.config.settings import settings
from app.modules.workspaces import service as service_module
from app.modules.workspaces.repository import WorkspaceRecord, WorkspaceRepository
from app.modules.workspaces.schemas import (
    CreateWorkspaceOut,
    CreateWorkspaceRequest,
    WorkspaceOut,
)
from app.modules.workspaces.service import WorkspaceService, _generate_slug
from app.shared.errors.app_error import ConflictError
from app.shared.middleware.authenticate import AuthContext


# ── slug generation ──────────────────────────────────────────────────────────────

def test_slug_ascii_company_name() -> None:
    assert _generate_slug("Riverline") == "riverline"


def test_slug_spaces_become_hyphens() -> None:
    assert _generate_slug("River Line Inc") == "river-line-inc"


def test_slug_special_chars_stripped() -> None:
    assert _generate_slug("River & Line! Co.") == "river-line-co"


def test_slug_unicode_normalized() -> None:
    assert _generate_slug("Société Générale") == "societe-generale"


def test_slug_leading_trailing_hyphens_stripped() -> None:
    result = _generate_slug("  --Acme--  ")
    assert not result.startswith("-")
    assert not result.endswith("-")


# ── fakes ────────────────────────────────────────────────────────────────────────

class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        pass


# Realistic asyncpg-style messages: production discriminates slug collisions
# from other constraint violations by the constraint name carried in ``orig``.
_SLUG_VIOLATION = Exception(
    'duplicate key value violates unique constraint "workspaces_slug_key"'
)
_FK_VIOLATION = Exception(
    'insert or update on table "workspaces" violates foreign key constraint '
    '"workspaces_created_by_fkey"'
)


class _FakeWorkspaceRepository(WorkspaceRepository):
    def __init__(
        self, *, fail_first: bool = False, fail_orig: Exception = _SLUG_VIOLATION
    ) -> None:
        self._fail_first = fail_first
        self._fail_orig = fail_orig
        self._create_call_count = 0
        self.created_workspace: WorkspaceRecord | None = None
        self.created_member: dict[str, Any] | None = None
        self.created_use_cases: tuple[list[str] | None, str | None] | None = None

    async def create_workspace(
        self,
        session: Any,
        *,
        id: str,
        name: str,
        slug: str,
        team_size: str,
        primary_use_case: str,
        use_cases: list[str] | None = None,
        use_case_other: str | None = None,
        created_by: str,
    ) -> WorkspaceRecord:
        self._create_call_count += 1
        if self._fail_first and self._create_call_count == 1:
            raise IntegrityError("INSERT INTO workspaces ...", {}, self._fail_orig)
        self.created_use_cases = (use_cases, use_case_other)
        self.created_workspace = WorkspaceRecord(id=id, name=name, slug=slug, plan="trial")
        return self.created_workspace

    async def create_member(
        self, session: Any, *, id: str, workspace_id: str, user_id: str, role: str
    ) -> None:
        self.created_member = {
            "id": id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": role,
        }

    async def update_onboarding(self, session: Any, **kwargs: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "get_session", _fake_get_session)


def _request(
    company_name: str = "Riverline",
    team_size: str = "51-200",
    use_case: str = "support",
    use_cases: list[str] | None = None,
    use_case_other: str | None = None,
) -> CreateWorkspaceRequest:
    return CreateWorkspaceRequest(
        company_name=company_name,
        team_size=team_size,
        primary_use_case=use_case,
        use_cases=use_cases,
        use_case_other=use_case_other,
    )


# ── service: workspace creation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_workspace_returns_trial_plan() -> None:
    service = WorkspaceService(repository=_FakeWorkspaceRepository())

    result = await service.create_workspace(_request(), user_id="usr_1", user_agent=None, ip=None)

    assert result.workspace.plan == "trial"


@pytest.mark.asyncio
async def test_create_workspace_slug_from_company_name() -> None:
    service = WorkspaceService(repository=_FakeWorkspaceRepository())

    result = await service.create_workspace(
        _request(company_name="Riverline"), user_id="usr_1", user_agent=None, ip=None
    )

    assert result.workspace.slug == "riverline"


@pytest.mark.asyncio
async def test_create_workspace_seeds_admin_member() -> None:
    repo = _FakeWorkspaceRepository()
    service = WorkspaceService(repository=repo)

    result = await service.create_workspace(_request(), user_id="usr_1", user_agent=None, ip=None)

    assert repo.created_member is not None
    assert repo.created_member["user_id"] == "usr_1"
    assert repo.created_member["role"] == "admin"
    assert repo.created_member["workspace_id"] == result.workspace.id


@pytest.mark.asyncio
async def test_create_workspace_access_token_has_workspace_and_role() -> None:
    from jose import jwt

    service = WorkspaceService(repository=_FakeWorkspaceRepository())

    result = await service.create_workspace(_request(), user_id="usr_1", user_agent=None, ip=None)

    claims: dict[str, Any] = jwt.decode(
        result.access_token, settings.jwt_access_secret, algorithms=["HS256"]
    )
    assert claims["sub"] == "usr_1"
    assert claims["workspace_id"] == result.workspace.id
    assert claims["role"] == "admin"
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_create_workspace_forwards_multi_select_use_cases() -> None:
    repo = _FakeWorkspaceRepository()
    service = WorkspaceService(repository=repo)

    await service.create_workspace(
        _request(
            use_cases=["support", "eng", "other"],
            use_case_other="Vendor security questionnaires",
        ),
        user_id="usr_1",
        user_agent=None,
        ip=None,
    )

    assert repo.created_use_cases == (
        ["support", "eng", "other"],
        "Vendor security questionnaires",
    )


@pytest.mark.asyncio
async def test_create_workspace_without_use_cases_forwards_none() -> None:
    """A client that only sends `primaryUseCase` still creates a workspace — the
    repository is what falls back to a one-element array."""
    repo = _FakeWorkspaceRepository()
    service = WorkspaceService(repository=repo)

    await service.create_workspace(_request(), user_id="usr_1", user_agent=None, ip=None)

    assert repo.created_use_cases == (None, None)


@pytest.mark.asyncio
async def test_slug_conflict_retried_with_suffix() -> None:
    repo = _FakeWorkspaceRepository(fail_first=True)
    service = WorkspaceService(repository=repo)

    result = await service.create_workspace(
        _request(company_name="Riverline"), user_id="usr_1", user_agent=None, ip=None
    )

    assert repo._create_call_count == 2
    # After suffix: "riverline-xxxx" where xxxx is 4 alphanumeric chars.
    assert result.workspace.slug.startswith("riverline-")
    assert len(result.workspace.slug) == len("riverline-") + 4


@pytest.mark.asyncio
async def test_non_slug_integrity_error_propagates() -> None:
    """A non-slug constraint violation must not be retried or mislabelled as a
    slug conflict — it propagates as-is rather than a misleading 409."""
    repo = _FakeWorkspaceRepository(fail_first=True, fail_orig=_FK_VIOLATION)
    service = WorkspaceService(repository=repo)

    with pytest.raises(IntegrityError):
        await service.create_workspace(
            _request(company_name="Riverline"), user_id="usr_1", user_agent=None, ip=None
        )
    # Not retried with a suffixed slug — it isn't a slug collision.
    assert repo._create_call_count == 1


# ── router: contract ─────────────────────────────────────────────────────────────

class _StubWorkspaceService:
    def __init__(
        self,
        *,
        result: CreateWorkspaceOut | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def create_workspace(
        self, body: Any, *, user_id: str, user_agent: Any, ip: Any
    ) -> CreateWorkspaceOut:
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result

    async def save_onboarding_step(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _workspace_out() -> CreateWorkspaceOut:
    return CreateWorkspaceOut(
        workspace=WorkspaceOut(id="wrk_1", name="Riverline", slug="riverline", plan="trial"),
        access_token="new.access.token",
    )


_PRE_WORKSPACE_AUTH = AuthContext(user_id="usr_1", workspace_id=None, role=None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


def _override(service: _StubWorkspaceService) -> None:
    from app.main import app
    from app.modules.workspaces.router import get_workspace_service
    from app.shared.middleware.authenticate import get_auth_context

    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[get_auth_context] = lambda: _PRE_WORKSPACE_AUTH


@pytest.mark.asyncio
async def test_post_workspaces_returns_201_envelope(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={"companyName": "Riverline", "teamSize": "51-200", "primaryUseCase": "support"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["workspace"]["plan"] == "trial"
    assert data["workspace"]["slug"] == "riverline"
    assert data["accessToken"] == "new.access.token"
    assert resp.json()["meta"]["requestId"]


@pytest.mark.asyncio
async def test_post_workspaces_accepts_multi_select_use_cases(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={
            "companyName": "Riverline",
            "teamSize": "51-200",
            "primaryUseCase": "support",
            "useCases": ["support", "product", "other"],
            "useCaseOther": "Vendor security questionnaires",
        },
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_post_workspaces_rejects_unknown_use_case_in_list(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={
            "companyName": "Riverline",
            "teamSize": "1-10",
            "primaryUseCase": "support",
            "useCases": ["support", "invalid"],
        },
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_rejects_overlong_use_case_other(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={
            "companyName": "Riverline",
            "teamSize": "1-10",
            "primaryUseCase": "other",
            "useCases": ["other"],
            "useCaseOther": "x" * 201,
        },
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_rejects_invalid_team_size(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={"companyName": "Riverline", "teamSize": "INVALID", "primaryUseCase": "support"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_rejects_invalid_use_case(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={"companyName": "Riverline", "teamSize": "1-10", "primaryUseCase": "invalid"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_rejects_unknown_fields(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={
            "companyName": "Riverline",
            "teamSize": "1-10",
            "primaryUseCase": "support",
            "hack": "value",
        },
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_rejects_empty_company_name(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=_workspace_out()))

    resp = await client.post(
        "/api/v1/workspaces",
        json={"companyName": "", "teamSize": "1-10", "primaryUseCase": "support"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_post_workspaces_requires_auth(client: AsyncClient) -> None:
    from app.main import app

    app.dependency_overrides.clear()

    resp = await client.post(
        "/api/v1/workspaces",
        json={"companyName": "Riverline", "teamSize": "1-10", "primaryUseCase": "support"},
    )

    assert resp.status_code == 401

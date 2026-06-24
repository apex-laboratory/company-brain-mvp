"""API key lifecycle business logic (BEST_PRACTICES §2 layering, §7, §8).

Framework-agnostic: the router passes the resolved ``AuthContext`` and the path
``workspace_id``; the service enforces membership, opens a tenant-scoped
transaction, and shapes the response. The admin-only guard is applied as a router
dependency (``require_role("admin")``) and backstopped by the ``api_keys_admin``
RLS policy (which gates SELECT, INSERT, and UPDATE to admins of the workspace).

Key generation (BEST_PRACTICES §7): a key is ``hph_live_<32 hex chars>``. Only a
SHA-256 hash and a short display ``prefix`` are stored; the raw key is returned
exactly once, in the creation response, and is unrecoverable afterwards.
"""
from __future__ import annotations

import secrets

from app.modules.api_keys.repository import ApiKeyRepository
from app.modules.api_keys.schemas import (
    ApiKeyCreated,
    ApiKeyCreateRequest,
    ApiKeySummary,
)
from app.shared.errors.app_error import NotFoundError
from app.shared.helpers.crypto import sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.authorize import assert_workspace_member
from app.shared.middleware.with_tenant import tenant_session

_KEY_PREFIX = "hph_live_"
_SECRET_HEX_CHARS = 32  # 16 random bytes → 32 hex chars (§7: hph_live_<32-char-random>)
_DISPLAY_CHARS = 4  # chars of the random part kept in the stored display prefix


def _generate_raw_key() -> tuple[str, str]:
    """Return ``(raw_key, display_prefix)``.

    ``raw_key`` is ``hph_live_<32 hex chars>``; ``display_prefix`` is
    ``hph_live_<first 4 chars>`` — enough for a human to recognise the key in a
    list without exposing the secret.
    """
    secret = secrets.token_hex(_SECRET_HEX_CHARS // 2)
    raw_key = f"{_KEY_PREFIX}{secret}"
    display_prefix = f"{_KEY_PREFIX}{secret[:_DISPLAY_CHARS]}"
    return raw_key, display_prefix


class ApiKeyService:
    def __init__(self, repository: ApiKeyRepository | None = None) -> None:
        self._repository = repository or ApiKeyRepository()

    async def list_keys(
        self, auth: AuthContext, workspace_id: str
    ) -> list[ApiKeySummary]:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            rows = await self._repository.list_keys(session, workspace_id)

        return [
            ApiKeySummary(
                id=row.id,
                name=row.name,
                prefix=row.prefix,
                scopes=row.scopes,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
            )
            for row in rows
        ]

    async def create_key(
        self, auth: AuthContext, workspace_id: str, body: ApiKeyCreateRequest
    ) -> ApiKeyCreated:
        workspace_id = assert_workspace_member(auth, workspace_id)

        raw_key, display_prefix = _generate_raw_key()
        key_id = generate_id("key")

        async with tenant_session(auth, workspace_id) as session:
            created_at = await self._repository.create_key(
                session,
                key_id=key_id,
                workspace_id=workspace_id,
                name=body.name,
                key_hash=sha256_hash(raw_key),
                key_prefix=display_prefix,
                scopes=body.scopes,
                created_by=auth.user_id,
            )
            await session.commit()

        return ApiKeyCreated(
            id=key_id,
            name=body.name,
            api_key=raw_key,  # returned once — never stored or retrievable again
            prefix=display_prefix,
            scopes=body.scopes,
            created_at=created_at,
        )

    async def revoke_key(
        self, auth: AuthContext, workspace_id: str, key_id: str
    ) -> None:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            revoked = await self._repository.revoke_key(session, workspace_id, key_id)
            await session.commit()

        if not revoked:
            raise NotFoundError("API key")

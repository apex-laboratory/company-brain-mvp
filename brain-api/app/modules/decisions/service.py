"""Decisions registry business logic (BACKEND_ASKS §8).

Read-only, RLS-scoped reads over the existing ``decisions`` table. Cursor
pagination mirrors the skills registry (keyset on ``(updated_at, id)``).
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime

from app.config.database import get_tenant_session
from app.modules.decisions.repository import DecisionsRepository
from app.modules.decisions.schemas import DecisionOut, DecisionOwner
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: decisions service called without workspace context — "
            "ensure require_role is declared on this route"
        )
    return auth.workspace_id, auth.role


class DecisionsService:
    def __init__(self, repository: DecisionsRepository | None = None) -> None:
        self._repo = repository or DecisionsRepository()

    async def list(
        self,
        auth: AuthContext,
        *,
        status: str | None,
        category: str | None,
        source: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[DecisionOut], str | None]:
        workspace_id, role = _require_workspace(auth)
        decoded = _decode_cursor(cursor)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            rows = await self._repo.list_decisions(
                session, status=status, category=category, source=source,
                limit=limit + 1, cursor=decoded,
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [_to_out(r) for r in page]
        next_cursor = (
            _encode_cursor(page[-1]["updated_at"], page[-1]["id"])
            if has_more and page
            else None
        )
        return items, next_cursor

    async def get(self, auth: AuthContext, decision_id: str) -> DecisionOut:
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            row = await self._repo.get_decision(session, decision_id)
        if row is None:
            raise NotFoundError("Decision")
        return _to_out(row)


def _to_out(r: dict) -> DecisionOut:
    owner = (
        DecisionOwner(name=r["owner_name"], avatar_color=r["owner_avatar_color"])
        if (r["owner_name"] or r["owner_avatar_color"])
        else None
    )
    return DecisionOut(
        id=r["id"],
        title=r["title"],
        provider=r["source_provider"],
        location=r["source_location"],
        status=r["status"],
        confidence=r["confidence"],
        category=r["category"],
        owner=owner,
        uses=int(r["monthly_uses"]),
        updated_at=r["updated_at"],
        body=r["summary"],
        rule=r["rule"],
    )


def _encode_cursor(updated_at: datetime, decision_id: str) -> str:
    raw = f"{updated_at.isoformat()}|{decision_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, decision_id = raw.split("|", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return ts, decision_id

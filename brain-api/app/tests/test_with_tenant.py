from __future__ import annotations

from typing import Any

import pytest

from app.shared.middleware.with_tenant import run_in_tenant


class _FakeSession:
    """Captures executed statements without touching a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self.calls.append((str(statement), params or {}))


@pytest.mark.asyncio
async def test_run_in_tenant_sets_all_three_gucs_transaction_local() -> None:
    session = _FakeSession()

    async with run_in_tenant(session, "wrk_1", "usr_1", "admin") as yielded:  # type: ignore[arg-type]
        assert id(yielded) == id(session)

    assert len(session.calls) == 1
    sql, params = session.calls[0]

    assert "set_config('app.current_workspace_id'" in sql
    assert "set_config('app.current_user_id'" in sql
    assert "set_config('app.current_role'" in sql
    # transaction-local flag (third arg `true`) — cannot bleed across connections.
    assert sql.count("true)") == 3

    assert params == {"workspace_id": "wrk_1", "user_id": "usr_1", "role": "admin"}

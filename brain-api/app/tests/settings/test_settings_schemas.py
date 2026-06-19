"""Schema validation tests for the settings + API key request models (KAN-64).

Covers the two validation rules that are easy to get subtly wrong: API-key scope
dedup (must run so duplicate-but-valid input is accepted, not rejected by a raw
length cap) and workspace ``domain`` normalization (blank → cleared/NULL, while
an explicit ``name: null`` is still rejected).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.api_keys.schemas import ApiKeyCreateRequest
from app.modules.workspaces.schemas import WorkspaceSettingsUpdate


# ── API key scopes ────────────────────────────────────────────────────────────
def test_duplicate_scopes_are_deduped_not_rejected() -> None:
    # 5 entries, all valid, deduping to 4 — must be accepted (regression: a raw
    # max_length cap would reject this before dedup ran).
    req = ApiKeyCreateRequest(
        name="Prod",
        scopes=[
            "brain:query",
            "skills:invoke",
            "sources:read",
            "decisions:read",
            "brain:query",
        ],
    )
    assert req.scopes == ["brain:query", "skills:invoke", "sources:read", "decisions:read"]


def test_empty_scopes_rejected() -> None:
    with pytest.raises(ValidationError):
        ApiKeyCreateRequest(name="Prod", scopes=[])


def test_invalid_scope_rejected() -> None:
    with pytest.raises(ValidationError):
        ApiKeyCreateRequest(name="Prod", scopes=["wat:nope"])


# ── workspace settings ─────────────────────────────────────────────────────────
def test_blank_domain_normalized_to_none() -> None:
    body = WorkspaceSettingsUpdate(domain="   ")
    fields = body.model_dump(exclude_unset=True)
    assert fields == {"domain": None}  # written as NULL, not ""


def test_explicit_null_domain_clears() -> None:
    body = WorkspaceSettingsUpdate(domain=None)
    assert body.model_dump(exclude_unset=True) == {"domain": None}


def test_omitted_fields_excluded() -> None:
    body = WorkspaceSettingsUpdate(name="Riverline")
    assert body.model_dump(exclude_unset=True) == {"name": "Riverline"}


def test_explicit_null_name_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkspaceSettingsUpdate(name=None)

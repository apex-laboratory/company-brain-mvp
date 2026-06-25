"""Shared Pydantic bases (BACKEND_BEST_PRACTICES.md §3, §14).

``CamelModel`` is the single response-schema base: it serializes to camelCase
(the API contract) while still accepting snake_case at construction. Every
module's response schemas inherit from it so the serialization contract lives in
one place.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Serialize to camelCase, accept snake_case at construction."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CamelRequestModel(BaseModel):
    """Request base: accept camelCase (or snake_case) input, reject unknown keys.

    The single source of truth for request-validation policy — accepts the
    camelCase JSON the API contract uses, still allows the snake_case field name,
    and rejects any unexpected key (mass-assignment defense). Module request
    schemas inherit this instead of re-declaring the config.
    """

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )

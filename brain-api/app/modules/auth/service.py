"""Auth business logic (BACKEND_BEST_PRACTICES.md §2 layering).

The service orchestrates the repository and integrations (email, OAuth). The
per-endpoint methods (signup, signin, OAuth exchange, refresh rotation, logout)
are implemented by the sibling endpoint tickets; this class establishes the
seam and dependency wiring they build on.
"""
from __future__ import annotations

from app.modules.auth.repository import AuthRepository


class AuthService:
    def __init__(self, repository: AuthRepository | None = None) -> None:
        self._repository = repository or AuthRepository()

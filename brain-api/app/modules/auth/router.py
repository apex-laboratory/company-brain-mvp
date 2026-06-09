"""Auth router.

Mounted under ``/api/v1/auth`` by ``main.py``. The eight Auth/Onboarding routes
(KAN-40) are added by the sibling endpoint tickets; this module owns the router
object and tag so the app boots with the auth surface wired in.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

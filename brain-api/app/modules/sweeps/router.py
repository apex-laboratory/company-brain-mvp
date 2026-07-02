"""Sweeps router (BACKEND_BEST_PRACTICES.md §2, §6).

Paths + dependencies only — logic lives in :class:`SweepsService`. Mounted under
``/api/v1`` by ``main.py``. Admin-only, like the source-connection routes: the
sweep reads via connections that are admin-scoped at the RLS layer.

* ``POST /sweeps`` — start the onboarding sweep (idempotent: returns the
  in-flight sweep if one exists).
* ``GET /sweeps/{sweep_id}`` — per-source progress for the onboarding screen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.modules.sweeps.service import SweepsService
from app.shared.http.respond import accepted, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role

router = APIRouter(prefix="/sweeps", tags=["sweeps"])

_service = SweepsService()


@router.post("", dependencies=[Depends(require_role("admin"))])
async def start_sweep(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """Kick off the onboarding sweep across all connected sources."""
    sweep, created = await _service.start(auth)
    payload = sweep.model_dump(by_alias=True)
    return accepted(request, payload) if created else ok(request, payload)


@router.get("/{sweep_id}", dependencies=[Depends(require_role("admin"))])
async def get_sweep(
    sweep_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """Sweep status with per-source progress."""
    sweep = await _service.get(auth, sweep_id)
    return ok(request, sweep.model_dump(by_alias=True))

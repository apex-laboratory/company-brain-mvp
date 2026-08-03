"""Sweeps router (BACKEND_BEST_PRACTICES.md §2, §6).

Paths + dependencies only — logic lives in :class:`SweepsService`. Mounted under
``/api/v1`` by ``main.py``. Admin-only, like the source-connection routes: the
sweep reads via connections that are admin-scoped at the RLS layer.

* ``POST /sweeps`` — start the onboarding sweep (idempotent: returns the
  in-flight sweep if one exists).
* ``GET /sweeps/active`` — the workspace's in-flight sweep, so a surface can
  re-attach to one it didn't start (reload mid-import).
* ``GET /sweeps/{sweep_id}`` — per-source progress for the onboarding screen.

A sweep scoped to specific connections (``POST /sources/{id}/backfill``) is the
same machinery with ``sweeps.config = {"source_ids": [...]}``; there is no
separate job or progress shape.
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


@router.get("/active", dependencies=[Depends(require_role("admin"))])
async def get_active_sweep(
    request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """The workspace's in-flight sweep, or ``null``.

    Declared before ``/{sweep_id}`` so "active" is matched as a literal path rather
    than captured as a sweep id.
    """
    sweep = await _service.get_active(auth)
    return ok(request, sweep.model_dump(by_alias=True) if sweep else None)


@router.get("/{sweep_id}", dependencies=[Depends(require_role("admin"))])
async def get_sweep(
    sweep_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """Sweep status with per-source progress."""
    sweep = await _service.get(auth, sweep_id)
    return ok(request, sweep.model_dump(by_alias=True))

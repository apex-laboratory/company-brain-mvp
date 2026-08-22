"""Agent-run ingestion router (Phase 7 — PRD Feature 29, §14).

``POST /runs`` is the only endpoint here, and it is **write-only**. The
``runs:write`` scope exists precisely so a credential that can feed the
self-improving loop cannot read the brain back out — a hook shim on a laptop and a
CI harness get push access without read access to the corpus.

There is deliberately no ``GET /runs``. A trace holds file contents, shell output
and customer records; the product's answer to "what did the agent do" is the
*distilled, reviewed procedure*, never the evidence it came from. Anything that
needs run-level facts after retention reads ``trace_digest`` through a dashboard
surface, under a JWT — not through an agent credential.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config.settings import settings
from app.modules.runs.schemas import RunIngestRequest
from app.modules.runs.service import RunsService
from app.shared.errors.app_error import PayloadTooLargeError
from app.shared.http.respond import accepted
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_scope
from app.shared.middleware.rate_limit import RUNS_INGEST_LIMIT, limiter, workspace_key

router = APIRouter(prefix="/runs", tags=["runs"])

_service = RunsService()


async def enforce_body_cap(request: Request) -> None:
    """Reject an oversize trace before FastAPI buffers and parses it.

    Checked from ``Content-Length`` rather than after parsing: a 4 MB trace that
    we parse and *then* reject has already cost the memory we were protecting.
    The message names the cap so the shim can split at a step boundary — see
    ``docs/AGENT_HOOK_SHIM.md``, which caps client-side for exactly this reason.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError:
        return
    if length > settings.run_max_body_bytes:
        raise PayloadTooLargeError(
            f"Trace exceeds {settings.run_max_body_bytes} bytes. Split the run at a "
            f"step boundary (max {settings.run_max_steps} steps per push) and "
            "re-send the parts under one externalId prefix."
        )


@router.post(
    "",
    dependencies=[Depends(require_scope("runs:write")), Depends(enforce_body_cap)],
)
@limiter.limit(RUNS_INGEST_LIMIT, key_func=workspace_key)
async def ingest_run(
    request: Request,
    body: RunIngestRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Push one agent run trace. ``202`` — gating and distillation are async.

    The response is a receipt, not a verdict: whether this run becomes a procedure
    depends on the success gate and on whether its task cluster has enough
    corroborating runs (Feature 30), neither of which is knowable in this request.
    """
    result = await _service.ingest(auth, body)
    return accepted(request, result.model_dump(by_alias=True))

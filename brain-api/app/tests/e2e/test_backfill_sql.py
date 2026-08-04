"""DB-level tests for the per-source backfill predicates (migration 0022).

Two pieces of logic here live in SQL and cannot be verified against a mocked
repository, yet both fail silently if wrong:

* ``SweepsRepository.find_covering`` — a scoped sweep must not be handed back as
  the answer to a request for a *different* source. If it were, clicking "Import
  history" on GitHub while a Slack import ran would return the Slack sweep, the
  UI would show progress, and GitHub's history would never be fetched.
* ``SourcesRepository.list_connections``'s ``needs_backfill`` — the rule deciding
  whether the button appears at all, including the one-hour ``syncing`` window
  that stops a dropped enqueue from hiding the button forever.

Runs only with ``E2E=1`` against a throwaway database (see conftest).
"""
from __future__ import annotations

import json

from sqlalchemy import text

from app.config.database import get_session
from app.modules.sources.repository import SourcesRepository
from app.modules.sweeps.repository import SweepsRepository

WS = "wrk_bf"
USER = "usr_bf"

_sweeps = SweepsRepository()
_sources = SourcesRepository()


async def _reset() -> None:
    async with get_session() as session:
        await session.execute(
            text("TRUNCATE sweeps, source_connections, workspaces, users CASCADE")
        )
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)").bindparams(
                id=USER, email="bf@example.com"
            )
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, created_by) "
                "VALUES (:id, 'BF', 'bf', :user)"
            ).bindparams(id=WS, user=USER)
        )
        await session.commit()


async def _connection(
    source_id: str,
    provider: str = "slack",
    *,
    status: str = "connected",
    sync_status: str = "pending",
    backfilled: bool = False,
    syncing_age_minutes: int = 0,
) -> None:
    """Insert one connection. ``syncing_age_minutes`` back-dates ``updated_at``."""
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO source_connections
                    (id, workspace_id, provider, name, status, sync_status,
                     backfilled_at, updated_at)
                VALUES (:id, :ws, CAST(:provider AS source_provider), :provider,
                        CAST(:status AS source_status), CAST(:sync AS sync_status),
                        CASE WHEN :backfilled THEN now() ELSE NULL END,
                        now() - make_interval(mins => :age))
                """
            ).bindparams(
                id=source_id, ws=WS, provider=provider, status=status,
                sync=sync_status, backfilled=backfilled, age=syncing_age_minutes,
            )
        )
        await session.commit()


async def _sweep(source_ids: list[str] | None, status: str = "running") -> str:
    async with get_session() as session:
        row = await _sweeps.create(
            session,
            workspace_id=WS,
            triggered_by=USER,
            config={"source_ids": source_ids} if source_ids else None,
        )
        if status != "pending":
            await session.execute(
                text("UPDATE sweeps SET status = :s WHERE id = :id").bindparams(
                    s=status, id=row["id"]
                )
            )
        await session.commit()
    return str(row["id"])


async def _covering(source_ids: list[str] | None) -> dict | None:
    async with get_session() as session:
        return await _sweeps.find_covering(session, source_ids)


async def _needs_backfill() -> dict[str, bool]:
    async with get_session() as session:
        rows = await _sources.list_connections(session)
    return {r["id"]: r["needs_backfill"] for r in rows}


# ── find_covering ─────────────────────────────────────────────────────────────
async def test_scoped_sweep_does_not_cover_a_different_source() -> None:
    await _reset()
    await _sweep(["src_slack"])
    # The whole point: GitHub's request must fall through to creating its own sweep.
    assert await _covering(["src_github"]) is None


async def test_scoped_sweep_covers_its_own_source() -> None:
    await _reset()
    sweep_id = await _sweep(["src_slack"])
    found = await _covering(["src_slack"])
    assert found is not None and str(found["id"]) == sweep_id


async def test_unscoped_sweep_covers_every_request() -> None:
    await _reset()
    sweep_id = await _sweep(None)
    for request in (["src_slack"], ["src_github"], None):
        found = await _covering(request)
        assert found is not None and str(found["id"]) == sweep_id, f"request={request!r}"


async def test_scoped_sweep_does_not_cover_a_workspace_wide_request() -> None:
    await _reset()
    await _sweep(["src_slack"])
    # An onboarding sweep must still be creatable while a single-source import runs,
    # or one stuck import would block the workspace from ever building its brain.
    assert await _covering(None) is None


async def test_multi_source_scope_covers_a_subset() -> None:
    await _reset()
    sweep_id = await _sweep(["src_slack", "src_github"])
    found = await _covering(["src_github"])
    assert found is not None and str(found["id"]) == sweep_id


async def test_finished_sweep_covers_nothing() -> None:
    await _reset()
    await _sweep(["src_slack"], status="completed")
    assert await _covering(["src_slack"]) is None


# ── needs_backfill ────────────────────────────────────────────────────────────
async def test_never_imported_connection_needs_backfill() -> None:
    await _reset()
    await _connection("src_slack")
    assert (await _needs_backfill())["src_slack"] is True


async def test_imported_connection_does_not_need_backfill() -> None:
    await _reset()
    await _connection("src_slack", backfilled=True, sync_status="healthy")
    assert (await _needs_backfill())["src_slack"] is False


async def test_disconnected_connection_does_not_offer_an_import() -> None:
    await _reset()
    await _connection("src_slack", status="disconnected")
    assert (await _needs_backfill())["src_slack"] is False


async def test_import_in_flight_suppresses_the_button() -> None:
    await _reset()
    await _connection("src_slack", sync_status="syncing")
    assert (await _needs_backfill())["src_slack"] is False


async def test_stale_syncing_becomes_actionable_again() -> None:
    await _reset()
    # The enqueue was dropped (queue.py swallows Redis outages) and the sweep never
    # ran. Past the one-hour window the connection must offer its import again
    # instead of sitting 'syncing' forever with no recovery path.
    await _connection("src_slack", sync_status="syncing", syncing_age_minutes=90)
    assert (await _needs_backfill())["src_slack"] is True


async def test_errored_connection_still_reports_its_missing_history() -> None:
    await _reset()
    # sync_status='error' is a failed run, not a completed import: the button stays,
    # since retrying is exactly what the user should be able to do.
    await _connection("src_slack", sync_status="error")
    assert (await _needs_backfill())["src_slack"] is True


# ── read report: event rollup + discard breakdown ─────────────────────────────
async def _event(
    source_id: str | None,
    outcome: str,
    *,
    stage: str | None = None,
    reason: str | None = None,
    ext: str = "",
) -> None:
    """Insert one source_event. ``source_id=None`` mimics a deleted connection."""
    meta = json.dumps({"stage": stage, "reason": reason}) if stage else "{}"
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO source_events
                    (workspace_id, provider, event_type, external_event_id,
                     payload, outcome, source_connection_id, pipeline_meta)
                VALUES (:ws, 'github', 'pr', :ext, '{}'::jsonb, :outcome,
                        :src, CAST(:meta AS jsonb))
                """
            ).bindparams(
                ws=WS, ext=ext or f"{outcome}-{stage}-{source_id}-{id(object())}",
                outcome=outcome, src=source_id, meta=meta,
            )
        )
        await session.commit()


async def _totals(source_id: str) -> dict:
    async with get_session() as session:
        return await _sources.connection_event_totals(session, source_id)


async def _rollup() -> dict[str, dict]:
    async with get_session() as session:
        rows = await _sources.list_connections(session)
    return {r["id"]: r for r in rows}


async def test_rollup_sorts_each_outcome_into_the_right_bucket() -> None:
    await _reset()
    await _connection("src_a")
    # published/review/draft all produced a skill; the rest did not.
    for outcome in ("published", "review", "draft"):
        await _event("src_a", outcome)
    await _event("src_a", "discarded", stage="relevance_gate", reason="one-off task")
    await _event("src_a", "queued")

    totals = await _totals("src_a")
    assert totals["items_read"] == 5
    assert totals["skills_kept"] == 3       # published + review + draft
    assert totals["discarded"] == 1
    assert totals["pending_items"] == 1


async def test_rollup_excludes_events_from_deleted_connections() -> None:
    await _reset()
    await _connection("src_a")
    await _event("src_a", "draft")
    # ON DELETE SET NULL residue from a connection that was removed. Counting these
    # would attribute another connection's history to this live source.
    for i in range(3):
        await _event(None, "discarded", stage="relevance_gate", ext=f"orphan-{i}")

    assert (await _totals("src_a"))["items_read"] == 1
    assert (await _rollup())["src_a"]["items_read"] == 1


async def test_rollup_reports_zero_not_null_for_a_source_with_no_events() -> None:
    # LEFT JOIN yields NULL without the COALESCE, which would break the schema's int.
    await _reset()
    await _connection("src_quiet")
    row = (await _rollup())["src_quiet"]
    assert (row["items_read"], row["skills_kept"], row["discarded"]) == (0, 0, 0)


async def test_breakdown_groups_by_stage_ordered_by_count() -> None:
    await _reset()
    await _connection("src_a")
    for i in range(4):
        await _event(
            "src_a", "discarded", stage="relevance_gate", reason=f"reason {i}", ext=f"r{i}"
        )
    await _event("src_a", "discarded", stage="skill_extractor", reason="abstained", ext="x1")

    async with get_session() as session:
        groups = await _sources.discard_breakdown(session, "src_a")
    assert [(g["stage"], g["count"]) for g in groups] == [
        ("relevance_gate", 4),
        ("skill_extractor", 1),
    ]


async def test_breakdown_caps_sample_reasons_at_three() -> None:
    # Reasons are free-text and near-unique; the report shows a few, not all of them.
    await _reset()
    await _connection("src_a")
    for i in range(9):
        await _event(
            "src_a", "discarded", stage="relevance_gate", reason=f"distinct {i}", ext=f"s{i}"
        )

    async with get_session() as session:
        groups = await _sources.discard_breakdown(session, "src_a")
    assert groups[0]["count"] == 9
    assert len(groups[0]["sample_reasons"]) == 3


async def test_breakdown_ignores_non_discarded_outcomes() -> None:
    await _reset()
    await _connection("src_a")
    await _event("src_a", "draft", stage="skill_writer", reason="kept")
    await _event("src_a", "failed", stage="skill_extractor", reason="boom")

    async with get_session() as session:
        groups = await _sources.discard_breakdown(session, "src_a")
    assert groups == []

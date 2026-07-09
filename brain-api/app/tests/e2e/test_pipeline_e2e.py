"""Phase 3 end-to-end acceptance suite (PRD "Phase 3 — Extraction Engine").

Drives the real stack — Postgres+pgvector via migrations, JobsRepository ingest,
``sweep_extract``/``extract_event``, orchestrator + stages + retry, pgvector
boundary search, and the reviews HTTP API — with only the LLM/embedding network
transports scripted (see conftest for the marker DSL).

PRD acceptance criteria covered:
* sweep produces skills in the review queue
* duplicates classified DUPLICATE **during a sweep** against a still-pending skill
* a contradiction detected and routed to review with both sources populated
* every written skill has a non-null embedding
* a forced transient LLM failure retries and succeeds
* a forced permanent failure lands in ``outcome='failed'`` and shows in sweep progress
* sweep cost rollup reported
* approval via the API publishes; rejection demotes; stats expose the rejection rate
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config.database import get_session
from app.integrations.base import RawItem
from app.integrations.slack import SlackIntegration
from app.jobs.repository import JobsRepository
from app.jobs.tasks.extract_event import extract_event
from app.jobs.tasks.sweep_extract import sweep_extract
from app.shared.middleware.authenticate import AuthContext, get_auth_context

WS = "wrk_e2e"
USER = "usr_e2e"

_slack = SlackIntegration()
_jobs = JobsRepository()


# ── seeding helpers ───────────────────────────────────────────────────────────
async def _reset_db() -> None:
    """Wipe pipeline-related tables (dedicated E2E database; see conftest guard)."""
    async with get_session() as session:
        await session.execute(
            text(
                "TRUNCATE source_events, skill_versions, reviews, skills, sweeps, "
                "source_connections, workspaces, users CASCADE"
            )
        )
        await session.commit()


async def _seed_workspace() -> str:
    """User + workspace + one running sweep; returns the sweep id."""
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)").bindparams(
                id=USER, email="e2e@example.com"
            )
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, created_by) "
                "VALUES (:id, 'E2E', 'e2e', :user)"
            ).bindparams(id=WS, user=USER)
        )
        sweep_id = (
            await session.execute(
                text(
                    "INSERT INTO sweeps (workspace_id, status, triggered_by) "
                    "VALUES (:ws, 'running', :user) RETURNING id"
                ).bindparams(ws=WS, user=USER)
            )
        ).scalar_one()
        await session.commit()
    return str(sweep_id)


def _slack_message(ts: str, text_: str) -> dict:
    return {
        "type": "message", "text": text_, "ts": ts,
        "channel": "C-e2e", "user": "U-e2e", "team": "T-e2e",
    }


async def _ingest(ts: str, content: str, sweep_id: str | None) -> str:
    """Normalize through the real Slack connector and insert like the sync path."""
    event = _slack.normalize(RawItem(external_id=ts, payload=_slack_message(ts, content)))
    async with get_session() as session:
        event_id = await _jobs.insert_event(session, WS, event, sweep_id=sweep_id)
        await session.commit()
    assert event_id, "seed event unexpectedly deduplicated"
    return event_id


def _skill_marker(
    name: str, logic_markers: str, *, confidence: float = 0.9, trigger: str = "e2e trigger"
) -> str:
    draft = {
        "name": name,
        "trigger": trigger,
        "base_logic": logic_markers,
        "extraction_confidence": confidence,
        "exceptions": [],
        "actions": [],
    }
    return f"SKILL<<{json.dumps(draft)}>>"


async def _event_row(event_id: str) -> dict:
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT outcome, processed, skill_id, attempts, pipeline_meta "
                    "FROM source_events WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=event_id)
            )
        ).mappings().first()
    assert row is not None
    return dict(row)


async def _one(sql: str, **params) -> dict:
    async with get_session() as session:
        row = (
            (await session.execute(text(sql).bindparams(**params))).mappings().first()
        )
    assert row is not None, f"no row for: {sql}"
    return dict(row)


# ── HTTP client for the reviews API ──────────────────────────────────────────
@pytest_asyncio.fixture
async def api() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=USER, workspace_id=WS, role="admin", scopes=[], kind="jwt"
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://e2e") as client:
            yield client
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


# ── the acceptance scenario ───────────────────────────────────────────────────
async def test_sweep_to_approval_full_flow(e2e_stubs: dict, api: AsyncClient) -> None:
    await _reset_db()
    sweep_id = await _seed_workspace()

    # E1 — a brand-new refund policy → review-status skill (sweeps never publish).
    e1 = await _ingest(
        "1720000000.000001",
        "DECISION: refunds within 30 days. "
        + _skill_marker("Refund policy", "Refunds within 30 days. vec=refunds:1.0 BOUNDARY=NEW"),
        sweep_id,
    )
    # E2 — the same policy restated → must dedupe against E1's *pending* skill.
    e2 = await _ingest(
        "1720000000.000002",
        "DECISION: 30-day refund window, restated. "
        + _skill_marker(
            "Refund policy restated",
            "Refund window is 30 days. vec=refunds:0.95 BOUNDARY=DUPLICATE",
        ),
        sweep_id,
    )
    # E3 — chatter → discarded at the relevance gate.
    e3 = await _ingest("1720000000.000003", "anyone up for lunch?", sweep_id)
    # E4 — an opposing policy → UPDATE route, contradiction detected → review card.
    e4 = await _ingest(
        "1720000000.000004",
        "DECISION: no refunds at all. "
        + _skill_marker(
            "No refunds", "No refunds ever. [CONTRA] vec=refunds:0.90 BOUNDARY=UPDATE"
        ),
        sweep_id,
    )
    # E5 — transient LLM blip on the gate; retry must recover and produce a skill.
    e5 = await _ingest(
        "1720000000.000005",
        "FLAKY DECISION: escalate P1s after 15 minutes. "
        + _skill_marker(
            "P1 escalation", "Escalate P1 after 15m. vec=escalation:1.0 BOUNDARY=NEW"
        ),
        sweep_id,
    )
    # E6 — permanent LLM failure → dead-letter, visible in sweep progress.
    e6 = await _ingest(
        "1720000000.000006",
        "PERMFAIL DECISION: this event can never process. "
        + _skill_marker("Never", "vec=never:1.0 BOUNDARY=NEW"),
        sweep_id,
    )

    tally = await sweep_extract({}, WS, sweep_id)

    assert tally == {
        "processed": 6, "published": 0, "review": 2, "draft": 0,
        "discarded": 1, "duplicates": 1, "contradictions": 1, "failed": 1,
    }

    # E1: review-status skill with a non-null embedding.
    row1 = await _event_row(e1)
    assert row1["outcome"] == "review" and row1["skill_id"]
    skill1 = await _one(
        "SELECT id, status, base_logic, version, confidence, embedding IS NOT NULL AS has_vec "
        "FROM skills WHERE id = :id", id=row1["skill_id"],
    )
    assert skill1["status"] == "review" and skill1["has_vec"] is True

    # E2: deduped against E1's *pending* skill (PRD sweep-scope rule) — the
    # published-only scope would have found nothing and flooded the queue.
    row2 = await _event_row(e2)
    assert row2["outcome"] == "duplicate"
    assert row2["skill_id"] == row1["skill_id"]
    dup_sources = await _one(
        "SELECT source_ids FROM skills WHERE id = :id", id=row1["skill_id"]
    )
    assert e2 in json.dumps(dup_sources["source_ids"])  # event appended as a source

    # E3: discarded at the gate.
    row3 = await _event_row(e3)
    assert row3["outcome"] == "discarded"
    assert row3["pipeline_meta"]["stage"] == "relevance_gate"

    # E4: contradiction review with BOTH sources populated; E1's logic untouched.
    row4 = await _event_row(e4)
    assert row4["outcome"] == "contradiction"
    contra = await _one(
        "SELECT id, kind, status, payload, after_text, skill_id FROM reviews "
        "WHERE kind = 'contradiction' AND workspace_id = :ws", ws=WS,
    )
    assert contra["skill_id"] == row1["skill_id"]
    assert contra["payload"]["source_a"]["excerpt"]
    assert contra["payload"]["source_b"]["excerpt"]
    untouched = await _one("SELECT base_logic FROM skills WHERE id = :id", id=row1["skill_id"])
    assert untouched["base_logic"] == skill1["base_logic"]

    # E5: the transient failure was retried (transport tripped once) and succeeded.
    row5 = await _event_row(e5)
    assert e2e_stubs.get("flaky_tripped") is True
    assert row5["outcome"] == "review" and row5["skill_id"]

    # E6: dead-lettered — visible, not silently dropped.
    row6 = await _event_row(e6)
    assert row6["outcome"] == "failed" and row6["processed"] is True
    assert row6["attempts"] == 1
    assert "RuntimeError" in row6["pipeline_meta"]["error"]

    # Cost telemetry: per-event stage costs + the sweep rollup.
    assert row1["pipeline_meta"]["costs"]["total_usd"] > 0
    assert "relevance_gate" in row1["pipeline_meta"]["costs"]["by_stage"]
    progress = await _one(
        "SELECT progress FROM sweeps WHERE id = CAST(:id AS uuid)", id=sweep_id
    )
    extraction = progress["progress"]["extraction"]
    assert extraction["processed"] == 6 and extraction["failed"] == 1
    assert extraction["cost_usd"] > 0

    # Every skill written by the sweep carries an embedding (PRD acceptance).
    async with get_session() as session:
        missing = (
            await session.execute(
                text("SELECT count(*) FROM skills WHERE workspace_id = :ws AND embedding IS NULL")
                .bindparams(ws=WS)
            )
        ).scalar_one()
    assert missing == 0

    # ── the review queue over the real HTTP API ──────────────────────────────
    resp = await api.get("/api/v1/reviews")
    assert resp.status_code == 200
    queue = resp.json()["data"]
    assert [r["kind"] for r in queue][0] == "contradiction"  # contradictions first
    kinds = {r["kind"] for r in queue}
    assert kinds == {"contradiction", "new_decision"}
    by_skill = {r["skillId"]: r for r in queue if r["kind"] == "new_decision"}
    review_e1 = by_skill[row1["skill_id"]]
    review_e5 = by_skill[row5["skill_id"]]

    # Approve E1's new_decision → active skill + v1 version row, human confidence.
    resp = await api.post(f"/api/v1/reviews/{review_e1['id']}/approve")
    assert resp.status_code == 200
    published = await _one(
        "SELECT status, confidence FROM skills WHERE id = :id", id=row1["skill_id"]
    )
    assert published["status"] == "active"
    version = await _one(
        "SELECT version, confidence FROM skill_versions WHERE skill_id = :id",
        id=row1["skill_id"],
    )
    assert version["version"] == "v1" and version["confidence"] == 1.0

    # Approve the contradiction → the new source's logic lands as v2, re-embedded.
    resp = await api.post(
        f"/api/v1/reviews/{contra['id']}/approve", json={"comment": "source B is right"}
    )
    assert resp.status_code == 200
    updated = await _one(
        "SELECT status, version, base_logic, embedding IS NOT NULL AS has_vec "
        "FROM skills WHERE id = :id", id=row1["skill_id"],
    )
    assert updated["status"] == "active"
    assert updated["version"] == "v2"
    assert updated["base_logic"] == contra["after_text"]
    assert updated["has_vec"] is True

    # Reject E5's new_decision → its review-status skill demotes to draft.
    resp = await api.post(f"/api/v1/reviews/{review_e5['id']}/reject")
    assert resp.status_code == 200
    demoted = await _one("SELECT status FROM skills WHERE id = :id", id=row5["skill_id"])
    assert demoted["status"] == "draft"

    # Double-resolve → 409; stats expose the PRD §15 rejection rate.
    resp = await api.post(f"/api/v1/reviews/{review_e5['id']}/approve")
    assert resp.status_code == 409
    resp = await api.get("/api/v1/reviews/stats")
    stats = resp.json()["data"]
    assert stats["approved"] == 2 and stats["rejected"] == 1
    assert stats["rejectionRate"] == pytest.approx(1 / 3, abs=1e-3)


async def test_live_path_auto_publishes_at_high_confidence(e2e_stubs: dict) -> None:
    """Non-sweep ``extract_event``: confidence ≥0.90 at ≥medium authority
    auto-publishes without a review row (webhook/poll path)."""
    await _reset_db()
    await _seed_workspace()
    event_id = await _ingest(
        "1720000001.000001",
        "DECISION: vendors are paid net-45. "
        + _skill_marker(
            "Vendor payment terms",
            "Vendor contracts are net-45. vec=vendors:1.0 BOUNDARY=NEW",
            confidence=0.98,
        ),
        None,  # not sweep-sourced
    )

    result = await extract_event({}, WS, event_id)
    assert result["outcome"] == "published"

    row = await _event_row(event_id)
    skill = await _one(
        "SELECT status, confidence, embedding IS NOT NULL AS has_vec FROM skills "
        "WHERE id = :id", id=row["skill_id"],
    )
    assert skill["status"] == "active"  # published immediately — no review row
    assert skill["has_vec"] is True
    version = await _one(
        "SELECT version, change_type FROM skill_versions WHERE skill_id = :id",
        id=row["skill_id"],
    )
    assert version["version"] == "v1"
    async with get_session() as session:
        reviews = (
            await session.execute(
                text("SELECT count(*) FROM reviews WHERE workspace_id = :ws").bindparams(ws=WS)
            )
        ).scalar_one()
    assert reviews == 0

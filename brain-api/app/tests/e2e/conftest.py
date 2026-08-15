"""E2E fixtures: real Postgres+pgvector, real pipeline code, scripted LLM transport.

These tests exercise the Phase 3 acceptance criteria end-to-end — real
migrations, real repositories, real orchestrator/stage/retry/JSON-parse code,
real pgvector similarity search, real reviews API. Only the outermost provider
transports are faked (``clients.get_provider().call`` and the OpenAI client
inside the embedder), so every line of pipeline code above the network runs
for real.

Run explicitly against a throwaway database (never production!):

    docker run -d --name brain-e2e-pg -e POSTGRES_PASSWORD=e2e \
        -p 55432:5432 pgvector/pgvector:pg16
    docker exec brain-e2e-pg psql -U postgres -c \
        "CREATE SCHEMA IF NOT EXISTS auth; CREATE OR REPLACE FUNCTION auth.uid() \
         RETURNS uuid LANGUAGE sql STABLE AS 'SELECT NULL::uuid';"
    DATABASE_URL=postgresql+asyncpg://postgres:e2e@localhost:55432/postgres \
        .venv/bin/python -m alembic upgrade head
    E2E=1 DATABASE_URL=postgresql+asyncpg://postgres:e2e@localhost:55432/postgres \
        .venv/bin/python -m pytest app/tests/e2e/ -q

Without ``E2E=1`` the whole directory is skipped, so the normal unit-test run
never needs a database.

## The scripted-LLM marker DSL

Synthetic Slack messages drive the pipeline deterministically via markers:

* ``DECISION:``            → the fake relevance gate answers *relevant*
* ``SKILL<<{json}>>``      → the fake extractor returns exactly this draft JSON
* in the draft's base_logic:
    ``vec=<topic>:<sim>``  → the fake embedder emits a vector whose cosine
                             similarity to the topic's base vector is ``<sim>``
                             (so pgvector search returns controlled results)
    ``BOUNDARY=<LABEL>``   → the fake boundary classifier returns this label
    ``[CONTRA]``           → the fake contradiction detector fires
* ``FLAKY``                → the transport raises ONE transient error first
                             (the real retry/backoff then recovers)
* ``PERMFAIL``             → the transport raises a permanent error
                             (the real dead-letter path must catch it)
"""
from __future__ import annotations

import json
import math
import os
import random
import re
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E") != "1",
    reason="E2E suite needs E2E=1 and a dedicated DATABASE_URL",
)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("E2E") != "1":
        skip = pytest.mark.skip(reason="E2E suite needs E2E=1 and a dedicated DATABASE_URL")
        for item in items:
            if "/e2e/" in str(item.fspath):
                item.add_marker(skip)


# The suite TRUNCATEs tables, so it must never run against a DB holding real
# data. Local databases are always allowed; a remote one (e.g. the Supabase
# project before launch, while it holds no live data) needs the explicit
# E2E_ALLOW_REMOTE=1 opt-in.
if os.environ.get("E2E") == "1":
    _url = os.environ.get("DATABASE_URL", "")
    _local = "localhost" in _url or "127.0.0.1" in _url
    assert _local or os.environ.get("E2E_ALLOW_REMOTE") == "1", (
        "E2E tests wipe tables. Point DATABASE_URL at a local throwaway database, "
        "or set E2E_ALLOW_REMOTE=1 if the remote DB genuinely holds no live data. Got: "
        + _url
    )


# ── deterministic embedding vectors ──────────────────────────────────────────
_DIM = 1536


def _unit(seed: str) -> list[float]:
    rng = random.Random(seed)
    v = [rng.gauss(0, 1) for _ in range(_DIM)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def vector_for(text: str) -> list[float]:
    """Vector with an exact cosine similarity to its topic's base vector.

    ``vec=<topic>:<sim>`` in the text controls the geometry; text without a
    marker gets a deterministic hash vector (dissimilar to everything else).
    """
    m = re.search(r"vec=(\w+):([\d.]+)", text)
    if not m:
        return _unit(f"fallback::{text}")
    topic, sim = m.group(1), float(m.group(2))
    base = _unit(f"topic::{topic}")
    if sim >= 0.999:
        return base
    # v = s·b + √(1−s²)·u with u ⊥ b  →  cos(v, b) = s exactly.
    raw = _unit(f"ortho::{topic}::{sim}")
    dot = sum(r * b for r, b in zip(raw, base, strict=True))
    ortho = [r - dot * b for r, b in zip(raw, base, strict=True)]
    norm = math.sqrt(sum(x * x for x in ortho))
    ortho = [x / norm for x in ortho]
    coef = math.sqrt(1 - sim * sim)
    return [sim * b + coef * o for b, o in zip(base, ortho, strict=True)]


# ── scripted transports ───────────────────────────────────────────────────────
class TransientBlip(Exception):
    """Looks transient to ``is_transient`` (module faked below via status_code)."""

    status_code = 429
    response = None


def _fake_gemini_call(state: dict):
    """All five pipeline stages route through the same configured provider, so
    one fake transport dispatches on system prompt across relevance/decision/
    boundary/extractor/contradiction — mirroring the provider's single
    ``call`` method."""
    from app.pipeline.prompts import boundary_classifier as bc_p
    from app.pipeline.prompts import contradiction_detector as cd_p
    from app.pipeline.prompts import decision_identifier as di_p
    from app.pipeline.prompts import relevance_gate as rg_p
    from app.pipeline.prompts import skill_extractor as se_p

    async def call(system: str, user: str, *, max_tokens: int):
        state["gemini_calls"] = state.get("gemini_calls", 0) + 1
        if system == rg_p.SYSTEM:
            if "FLAKY" in user and not state.get("flaky_tripped"):
                state["flaky_tripped"] = True
                raise TransientBlip("simulated 429")
            if "PERMFAIL" in user:
                raise RuntimeError("simulated permanent model failure")
            return (
                json.dumps({"relevant": "DECISION:" in user, "reason": "e2e-scripted"}),
                12, 4,
            )
        if system == di_p.SYSTEM:
            # One decision whose text is the full prompt — it carries the
            # SKILL<<…>> marker through to the extractor stage.
            return (
                json.dumps({
                    "decisions": [{
                        "message_id": "m-e2e", "author": "e2e", "timestamp": "t",
                        "decision_text": user,
                    }]
                }),
                15, 8,
            )
        if system == bc_p.SYSTEM:
            m = re.search(r"BOUNDARY=([A-Z]+)", user)  # draft's marker comes first
            return json.dumps({"classification": m.group(1) if m else "NEW"}), 10, 4
        if system == se_p.SYSTEM:
            m = re.search(r"SKILL<<(.*?)>>", user, re.S)
            if not m:
                return json.dumps({"trigger": "", "base_logic": ""}), 20, 6
            return m.group(1), 20, 12  # real _parse_json validates it
        if system == cd_p.SYSTEM:
            return json.dumps({"has_contradiction": "[CONTRA]" in user}), 14, 4
        raise AssertionError(f"unexpected gemini system prompt: {system[:60]!r}")

    return call


class _FakeEmbeddings:
    async def create(self, model: str, input: str):  # noqa: A002 — SDK signature
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=vector_for(input))],
            usage=SimpleNamespace(prompt_tokens=7),
        )


class _FakeOpenAI:
    embeddings = _FakeEmbeddings()


# semaphore_limit 1 → the sweep processes events strictly in insertion order,
# so later events deterministically see the skills earlier ones created (the
# sweep-scope dedupe assertions depend on that ordering).
_AUTHORITY_YAML = """\
tiers:
  high:
    weight: 1.0
    sources:
      - type: slack
        signals: []
      - type: github
        signals: []
routing: {}
sweep:
  rate_per_minute: 60000
  semaphore_limit: 1
"""


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Dispose the pooled engine before each test — pytest-asyncio gives every
    test a fresh event loop, and asyncpg connections are loop-bound."""
    from app.config.database import engine

    await engine.dispose()
    yield


@pytest.fixture
def llm_state() -> dict:
    return {}


@pytest.fixture
def e2e_stubs(monkeypatch: pytest.MonkeyPatch, tmp_path, llm_state: dict):
    """Wire the scripted transports + embedder + a high-authority test YAML."""
    from app.pipeline import authority as authority_mod
    from app.pipeline import cache as cache_mod
    from app.pipeline import embedder
    from app.pipeline.llm import clients

    fake_provider = SimpleNamespace(model="fake-gemini", call=_fake_gemini_call(llm_state))
    monkeypatch.setattr(clients, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(embedder, "openai_client", lambda: _FakeOpenAI())

    async def _no_redis():
        raise ConnectionError("no redis in the e2e environment")

    # invalidate_skills is best-effort by contract — prove it by removing Redis.
    monkeypatch.setattr(cache_mod, "init_redis", _no_redis)

    yaml_path = tmp_path / "source_authority.yaml"
    yaml_path.write_text(_AUTHORITY_YAML)
    monkeypatch.setattr(
        authority_mod, "_annotator", authority_mod.AuthorityAnnotator(yaml_path)
    )
    yield llm_state
    authority_mod._annotator = None  # don't leak the test config

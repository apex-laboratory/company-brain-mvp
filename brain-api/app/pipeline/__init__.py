"""Extraction pipeline (Phase 3 — PRD §16).

Turns ingested ``source_events`` rows (``processed = false``) into structured,
versioned skills plus human-review rows. Stage flow:

    relevance_gate (Gemini) → context expander → decision_identifier (Gemini)
    → skill_extractor (Gemini*) → embedder (OpenAI) → boundary_classifier (Gemini)
    → contradiction_detector (Gemini*) → confidence_scorer → skill_writer

    (*) skill_extractor/contradiction_detector are temporarily on Gemini instead
    of Sonnet while ANTHROPIC_API_KEY is unavailable — see their docstrings.

The orchestrator (``orchestrator.run_pipeline``) sequences the stages; all DB
access goes through ``repository.PipelineRepository``; all LLM access goes
through ``llm.clients`` (retry + cost capture built in). ARQ entry points live
in ``app.jobs.tasks.extract_event``.
"""

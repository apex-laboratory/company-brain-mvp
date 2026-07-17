"""Extraction pipeline (Phase 3 — PRD §16).

Turns ingested ``source_events`` rows (``processed = false``) into structured,
versioned skills plus human-review rows. Stage flow:

    relevance_gate (Groq) → context expander → decision_identifier (Groq)
    → skill_extractor (Sonnet) → embedder (OpenAI) → boundary_classifier (Groq)
    → contradiction_detector (Sonnet) → confidence_scorer → skill_writer

The orchestrator (``orchestrator.run_pipeline``) sequences the stages; all DB
access goes through ``repository.PipelineRepository``; all LLM access goes
through ``llm.clients`` (retry + cost capture built in). ARQ entry points live
in ``app.jobs.tasks.extract_event``.
"""

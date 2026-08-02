"""Extraction-quality eval harness (Phase 3 deliverable, PRD §15 / Phase 3 acceptance).

Runs the *real* pipeline stage functions (real prompts, real LLM calls) over the
labeled synthetic datasets in ``evals/datasets/`` and scores them against the PRD
gates:

* relevance-gate precision ≥ 70%
* contradiction-detection recall ≥ 80%
* boundary-classification accuracy (reported; no hard PRD gate)

This is the regression suite for every future prompt change — run it before and
after touching anything in ``app/pipeline/prompts/``:

    cd brain-api && python -m evals            # all suites
    python -m evals --suite relevance          # one suite
    python -m evals --json                     # machine-readable report

Requires real ``GEMINI_API_KEY`` / ``ANTHROPIC_API_KEY`` in the environment — the
harness measures prompt quality, so there is nothing meaningful to run against
mocks. (The harness *logic* is unit-tested with fakes in ``app/tests/evals/``.)
"""

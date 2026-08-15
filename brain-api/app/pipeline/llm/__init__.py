"""LLM access layer for the extraction pipeline.

Stages never import provider SDKs directly — they call ``clients.llm_json`` /
``clients.llm_stream`` / ``app.pipeline.embedder.embed_text``, which bundle
retry (``retry.with_retries``) and cost capture (``pricing.cost_usd``). The
provider behind ``llm_json``/``llm_stream`` is selected at runtime by
``settings.llm_provider`` (``LLM_PROVIDER`` env var) via
``providers.get_provider`` — swapping Gemini/Anthropic/OpenRouter needs no
code changes.
"""

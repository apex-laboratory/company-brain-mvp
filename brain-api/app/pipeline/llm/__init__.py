"""LLM access layer for the extraction pipeline.

Stages never import provider SDKs directly — they call ``clients.groq_json`` /
``clients.sonnet_json`` / ``app.pipeline.embedder.embed_text``, which bundle
retry (``retry.with_retries``) and cost capture (``pricing.cost_usd``).
"""

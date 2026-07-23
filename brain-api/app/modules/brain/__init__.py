"""Brain chat delivery module (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

The dashboard-reachable "Ask the brain" surface: a readiness gate, a JWT/API-key
REST endpoint, and grounded answer synthesis over the human-reviewed skills index.
Retrieval is reused from the pipeline (``PipelineRepository.similar_skills``); this
module adds the generation half, the gate, and chat persistence.
"""

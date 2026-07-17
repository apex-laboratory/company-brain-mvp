"""Pipeline stages. Each stage is a pure async function over DTOs from
``app.pipeline.types``; LLM access only via ``app.pipeline.llm.clients``,
DB access only in ``skill_writer`` via the passed repository."""

async def embed_text(text: str) -> list[float]:
    """
    Generate a 1536-dimension embedding using OpenAI text-embedding-3-small.
    Called on skill trigger + base_logic at write time, and on agent query
    at retrieval time.
    """
    raise NotImplementedError("Phase 3")

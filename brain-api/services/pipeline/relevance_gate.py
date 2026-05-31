async def is_relevant(content: str, source: str) -> bool:
    """
    Groq: binary gate — does this content contain operational decision logic,
    a policy rule, a process instruction, or an exception to an existing rule?

    Returns True to proceed, False to discard (outcome=discarded).
    This is the cheapest call in the pipeline and filters most noise before
    any expensive processing.
    """
    raise NotImplementedError("Phase 3")

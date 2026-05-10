from graphiti_core import Graphiti
from config import settings

_graphiti: Graphiti | None = None


async def init_graphiti():
    global _graphiti
    _graphiti = Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    await _graphiti.build_indices_and_constraints()


async def get_graphiti() -> Graphiti:
    if _graphiti is None:
        raise RuntimeError("Graphiti not initialized")
    return _graphiti


async def check_neo4j_health() -> bool:
    if _graphiti is None:
        return False
    try:
        # Try a lightweight query through graphiti's neo4j driver
        driver = _graphiti.driver
        if hasattr(driver, "execute_query"):
            await driver.execute_query("RETURN 1 AS n")
        elif hasattr(driver, "_driver"):
            await driver._driver.verify_connectivity()
        return True
    except Exception:
        return False


async def close_graphiti():
    if _graphiti:
        await _graphiti.close()

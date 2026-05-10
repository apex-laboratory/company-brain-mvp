from pydantic import BaseModel, Field


# --- Entity types ---

class PolicyRule(BaseModel):
    """A discrete operational rule that governs an agent decision."""
    condition: str = Field(description="The condition under which this rule applies")
    action: str = Field(description="The action to execute when condition is met")
    confidence: float = Field(description="Extraction confidence 0.0-1.0")
    source_type: str = Field(description="zendesk | slack | notion | process_mining")


class CustomerTier(BaseModel):
    """A customer classification that gates specific rules or actions."""
    tier_name: str = Field(description="Name of the tier e.g. standard, enterprise, B2B")
    crm_field: str = Field(description="CRM field that stores this value", default="")


class ThresholdValue(BaseModel):
    """A numeric threshold that determines rule branching."""
    value: float = Field(description="The threshold number")
    unit: str = Field(description="Unit e.g. days, USD, percentage")
    context: str = Field(description="What this threshold applies to")


class ExceptionCondition(BaseModel):
    """A condition that overrides or modifies a base rule."""
    override_type: str = Field(description="time_window | policy | routing")
    trigger: str = Field(description="What triggers this exception")


# --- Edge types ---

class OverridesEdge(BaseModel):
    """Source rule overrides target rule when its condition is met."""
    priority: int = Field(description="Override priority — higher wins on conflict", default=1)


class HasExceptionForEdge(BaseModel):
    """Source policy has a defined exception path for target entity."""
    exception_action: str = Field(description="Action taken in the exception case")


class GovernsEdge(BaseModel):
    """Source rule governs the handling of target entity type."""
    scope: str = Field(description="Scope of governance e.g. time, amount, routing")


class RequiresRoutingToEdge(BaseModel):
    """Source entity type requires routing to target role or system."""
    routing_condition: str = Field(description="Condition that triggers routing")


ENTITY_TYPES = {
    "PolicyRule": PolicyRule,
    "CustomerTier": CustomerTier,
    "ThresholdValue": ThresholdValue,
    "ExceptionCondition": ExceptionCondition,
}

EDGE_TYPES = {
    "OVERRIDES": OverridesEdge,
    "HAS_EXCEPTION_FOR": HasExceptionForEdge,
    "GOVERNS": GovernsEdge,
    "REQUIRES_ROUTING_TO": RequiresRoutingToEdge,
}


async def ingest_to_graph(skill_name: str, content: str, source_id: str) -> dict:
    """Ingest an extracted skill into the Graphiti knowledge graph."""
    raise NotImplementedError("Phase 4")

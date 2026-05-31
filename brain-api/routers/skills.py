from fastapi import APIRouter, HTTPException
from models.schemas import SkillCreate, SkillUpdate

router = APIRouter()


@router.get("/search")
async def search_skills(q: str, limit: int = 5):
    """pgvector semantic search over published skills."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.get("/{skill_id}/versions")
async def get_versions(skill_id: str):
    """Full version history for a skill."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    """Full skill body by ID."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.post("")
async def create_skill(skill: SkillCreate):
    """
    Manual skill create. Sets changed_by=human_authored,
    confidence=1.0, status=published.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.patch("/{skill_id}")
async def update_skill(skill_id: str, update: SkillUpdate):
    """Manual skill edit."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")

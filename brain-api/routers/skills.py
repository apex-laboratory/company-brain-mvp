from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/search")
async def search_skills(q: str, limit: int = 5):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.get("/{skill_id}/versions")
async def get_versions(skill_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("")
async def create_skill():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.patch("/{skill_id}")
async def update_skill(skill_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")

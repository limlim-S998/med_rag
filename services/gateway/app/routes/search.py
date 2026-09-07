# POST /studies/{study_id}/search -> proxies to the retrieval service.
#
# Thin by design: authorise the study, forward the correlation header, return
# the hits. If this file ever grows retrieval logic, that logic is in the
# wrong service.

from fastapi import APIRouter

router = APIRouter(prefix="/studies", tags=["search"])


@router.post("/{study_id}/search")
async def search(study_id: str):
    ...

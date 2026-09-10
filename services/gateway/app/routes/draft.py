# Drafting. The streaming endpoint the writer actually sits in front of.
#
# retrieve -> rerank -> generate, streamed back token by token so text appears
# rather than arriving after a ten-second wait on a full section. The stream
# passes through untouched; the gateway does not buffer it, because buffering
# is the difference between "fast" and "the same latency with extra steps".
#
# On accept: core.section_draft.status moves to accepted with the writer's
# oid. That row is the record that a human is the author.

from fastapi import APIRouter

from medw_core.service import domain_unavailable

router = APIRouter(prefix="/studies", tags=["draft"])


@router.post("/{study_id}/sections/{section_path}/draft")
async def draft_section(study_id: str, section_path: str):
    # StreamingResponse over the generation service's stream.
    domain_unavailable()


@router.post("/{study_id}/sections/{section_path}/accept")
async def accept_section(study_id: str, section_path: str):
    domain_unavailable()

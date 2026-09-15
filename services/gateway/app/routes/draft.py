"""Acceptance remains a writer operation; NGINX routes drafting to generation.

The future acceptance handler records the writer's oid in the accepted draft.
"""

from fastapi import APIRouter

from medw_core.service import domain_unavailable

router = APIRouter(prefix="/studies", tags=["draft"])


@router.post("/{study_id}/sections/{section_path}/accept")
async def accept_section(study_id: str, section_path: str):
    domain_unavailable()

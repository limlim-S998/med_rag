"""Writer acceptance changes one saved draft, never its generation audit."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from medw_core import tracing
from medw_core.auth import Principal, study_user
from medw_core.persistence import Conflict

router = APIRouter(prefix="/studies", tags=["draft"])


class AcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: UUID


@router.post("/{study_id}/sections/{section_path}/accept")
async def accept_section(study_id: str, section_path: str, body: AcceptRequest,
                         request: Request, user: Principal = Depends(study_user)):
    try:
        return await request.app.state.services.require("drafts").accept(
            study_id, section_path, str(body.draft_id), user.oid, tracing.CORRELATION_ID.get())
    except LookupError as exc:
        raise HTTPException(404, "draft not found") from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc

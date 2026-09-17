"""Register bounded uploads and expose document metadata.

Clients upload directly to the single-blob SAS URL issued by Azure Blob Storage.
"""

from fastapi import APIRouter, HTTPException, Request

from medw_core.uploads import UploadRequest

router = APIRouter(prefix="/studies", tags=["documents"])


@router.post("/{study_id}/documents:upload-url", status_code=201)
async def upload_url(study_id: str, body: UploadRequest, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "upload registration unavailable")
    try:
        return await services.require("uploads").register(
            study_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{study_id}/documents")
async def list_documents(study_id: str, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "document metadata unavailable")
    return await services.require("documents").by_study(study_id)

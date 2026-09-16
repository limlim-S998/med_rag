"""Register bounded uploads and expose document metadata.

Azure clients upload directly to a single-blob SAS URL. The local adapter exposes
an equivalent expiring capability route backed by shared durable files.
"""

from fastapi import APIRouter, HTTPException, Query, Request, Response

from medw_core.persistence import Conflict
from medw_core.uploads import UploadRequest

router = APIRouter(prefix="/studies", tags=["documents"])
upload_router = APIRouter(prefix="/studies", tags=["uploads"])


@router.post("/{study_id}/documents:upload-url", status_code=201)
async def upload_url(study_id: str, body: UploadRequest, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "upload registration unavailable")
    try:
        return await services.require("uploads").register(
            study_id, body, public_base_url=(request.app.state.settings.public_base_url
                                            or str(request.base_url)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@upload_router.put("/{study_id}/uploads/{upload_id}", status_code=201, include_in_schema=False)
async def upload_bytes(study_id: str, upload_id: str, request: Request,
                       token: str = Query(min_length=32, max_length=256)):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "upload storage unavailable")
    uploads = services.require("uploads")
    # Bound before buffering, including chunked requests without Content-Length.
    payload = bytearray()
    async for part in request.stream():
        if len(payload) + len(part) > uploads.max_bytes:
            raise HTTPException(413, "file exceeds configured upload limit")
        payload.extend(part)
    try:
        await uploads.write_local(study_id, upload_id, token, bytes(payload))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "registered upload not found") from exc
    except (ValueError, Conflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=201)


@router.get("/{study_id}/documents")
async def list_documents(study_id: str, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "document metadata unavailable")
    return await services.require("documents").by_study(study_id)

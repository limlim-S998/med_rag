# Upload and ingestion status.
#
# Upload does not stream the file through this pod. The gateway issues a
# short-lived user-delegation SAS scoped to one blob path and the client PUTs
# directly to Blob Storage - a 300MB TFL package should not traverse a pod
# that is otherwise handling JSON. The SAS is delegated from the pod's managed
# identity, so there is still no account key anywhere.
#
# Then: notify the ingestion worker (single doc, synchronous - the writer is
# waiting) or trigger the Airflow DAG (bulk). Job state goes in Cosmos, which
# is what the status endpoint polls.

from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request

from medw_core.service import domain_unavailable

router = APIRouter(prefix="/studies", tags=["documents"])


@router.post("/{study_id}/documents:upload-url")
async def upload_url(study_id: str, filename: str):
    domain_unavailable()


@router.get("/{study_id}/documents")
async def list_documents(study_id: str, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "document metadata unavailable")
    return await services.require("documents").by_study(study_id)


@router.get("/{study_id}/jobs/{job_id}")
async def job_status(study_id: str, job_id: str, request: Request):
    # Study authorization has already run at the router boundary.
    base = request.app.state.settings.ingestion_url
    try:
        response = await request.app.state.http.get(
            f"{base}/jobs/{quote(study_id, safe='')}/{quote(job_id, safe='')}")
        if response.status_code == 404:
            raise HTTPException(404, "job not found")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "job status unavailable") from exc
    return response.json()

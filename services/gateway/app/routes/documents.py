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

from fastapi import APIRouter

router = APIRouter(prefix="/studies", tags=["documents"])


@router.post("/{study_id}/documents:upload-url")
async def upload_url(study_id: str, filename: str):
    ...


@router.get("/{study_id}/documents")
async def list_documents(study_id: str):
    # Cosmos, single-partition on /study_id.
    ...


@router.get("/{study_id}/jobs/{job_id}")
async def job_status(study_id: str, job_id: str):
    ...

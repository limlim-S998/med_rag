"""Accept registered uploads into a durable, recovering ingestion queue."""
import hashlib

from fastapi import FastAPI, HTTPException, Request, Response

from medw_core.context import CORRELATION_ID
from medw_core.ids import source_revision_id
from medw_core.persistence import Conflict
from medw_core.service import (
    add_platform_routes,
    attach_request_instrumentation,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings
from medw_core.uploads import IngestRequest

s = get_settings().model_copy(update={"service_name": "ingestion-worker"})
app = FastAPI(title="ingestion-worker", lifespan=lifespan_for("ingestion-worker", s))
attach_request_instrumentation(app, "ingestion-worker")
add_platform_routes(app, s)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


@app.post("/studies/{study_id}/documents/{doc_id}/ingest", status_code=202)
async def ingest(study_id: str, doc_id: str, body: IngestRequest, request: Request):
    # NGINX authorizes the study first; only the controller may reach this route.
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "ingestion dependencies unavailable")
    uploads, jobs = services.require("uploads"), services.require("jobs")
    try:
        registered = await uploads.state.get("upload", study_id, body.upload_id)
        if registered is None or registered.value["doc_id"] != doc_id:
            raise KeyError(body.upload_id)
        existing = await jobs.get(study_id, hashlib.sha256(body.idempotency_key.encode()).hexdigest())
        if existing:
            revision = source_revision_id(study_id, doc_id, registered.value["sha256"])
            if existing["doc_id"] != doc_id or existing["source_revision"] != revision:
                raise Conflict("idempotency key reused for different input")
            return existing
        source = await uploads.capture(study_id, doc_id, body.upload_id,
                                       services.require("evidence"))
        # Source bytes are immutable before the durable acknowledgement. No
        # request-local task is needed: every worker discovers persisted jobs.
        return await jobs.create(study_id, doc_id, source_revision=source.revision_id,
                                 idempotency_key=body.idempotency_key,
                                 correlation_id=CORRELATION_ID.get())
    except KeyError as exc:
        raise HTTPException(404, "registered upload not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, "registered file has not been uploaded") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, Conflict) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/studies/{study_id}/jobs/{job_id}")
async def job_status(study_id: str, job_id: str, request: Request):
    # NGINX performs the gateway access subrequest before forwarding here.
    # The chart restricts writer traffic to that controller.
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "job store unavailable")
    job = await services.require("jobs").get(study_id, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job

"""Ingestion service shell; durable synthetic jobs exercise the worker contract."""
from fastapi import FastAPI, HTTPException, Request, Response

from medw_core.composition import effective_settings
from medw_core.service import (
    add_platform_routes,
    domain_unavailable,
    instrument,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

s = effective_settings(get_settings().model_copy(update={"service_name": "ingestion-worker"}))
app = FastAPI(title="ingestion-worker", lifespan=lifespan_for("ingestion-worker", s))
instrument(app, s, "ingestion-worker")
add_platform_routes(app, s)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


@app.post("/ingest", status_code=202)
async def ingest(study_id: str, doc_id: str, blob_path: str):
    # Do not accept a job that has no installed processing implementation.
    domain_unavailable()


@app.get("/jobs/{study_id}/{job_id}")
async def job_status(study_id: str, job_id: str, request: Request):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "job store unavailable")
    job = await services.require("jobs").get(study_id, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job

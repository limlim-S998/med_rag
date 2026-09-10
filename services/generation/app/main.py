"""Generation shell: dependencies and provenance execute; medical prose is held back."""
from pathlib import Path

from fastapi import FastAPI, Request, Response

from medw_core.composition import effective_settings
from medw_core.service import (
    add_platform_routes,
    domain_unavailable,
    instrument,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

s = effective_settings(get_settings().model_copy(update={"service_name": "generation"}))
app = FastAPI(title="generation", lifespan=lifespan_for(
    "generation", s, prompts=Path(__file__).parent / "prompts"))
instrument(app, s, "generation")
add_platform_routes(app, s)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


@app.post("/draft")
async def draft(req: dict):
    domain_unavailable()

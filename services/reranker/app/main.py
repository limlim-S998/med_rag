"""HTTP boundary for the currently installed deterministic reranker."""
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from medw_core.service import (
    add_platform_routes,
    attach_request_instrumentation,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

s = get_settings().model_copy(update={"service_name": "reranker"})
app = FastAPI(title="reranker", lifespan=lifespan_for("reranker", s))
attach_request_instrumentation(app, "reranker")
add_platform_routes(app, s)


class Candidate(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=32000)


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=16000)
    candidates: list[Candidate] = Field(max_length=100)
    top_k: int = Field(default=8, ge=1, le=100)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


@app.post("/rerank")
async def rerank(req: RerankRequest, request: Request):
    service = getattr(request.app.state.services, "reranker", None)
    if service is None:
        raise HTTPException(503, "reranker unavailable")
    ranked = await service.rerank(
        req.query, [(candidate.id, candidate.text) for candidate in req.candidates], top_k=req.top_k)
    return {"results": [{"id": key, "score": score} for key, score in ranked],
            "implementation": "placeholder-overlap-1"}

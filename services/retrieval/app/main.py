"""Retrieval orchestration: select one published generation for both stores."""
import asyncio

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from medw_core import tracing
from medw_core.composition import effective_settings
from medw_core.persistence import Conflict
from medw_core.projections import SECTION_FIELD, TEXT_FIELD
from medw_core.schemas import Citation, Hit, RetrievalRequest, RetrievalResponse
from medw_core.service import (
    add_platform_routes,
    domain_unavailable,
    instrument,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

from .fusion import rrf
from .qdrant_repo import QdrantRepo
from .sparse_repo import SparseRepo

s = effective_settings(get_settings().model_copy(update={"service_name": "retrieval"}))
app = FastAPI(title="retrieval", lifespan=lifespan_for(
    "retrieval", s, vector_factory=QdrantRepo, sparse_factory=SparseRepo))
instrument(app, s, "retrieval")
add_platform_routes(app, s)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


async def retrieve_candidates(svc, req: RetrievalRequest):
    embedder = svc.require("embedder")
    generation = await svc.require("index_registry").select(
        req.study_id, embed_version=embedder.embed_version,
        embed_deployment=s.embed_deployment, dimensions=embedder.dimensions,
        embed_model_version=s.embed_model_version,
        embed_model_name=s.embed_model_name,
    )
    flt = req.to_filter().model_copy(update={"index_generation": generation})
    vector = (await embedder.embed([req.query]))[0]
    dense, sparse = await asyncio.gather(
        svc.require("vectors").search(vector, flt, limit=s.fusion_top_n),
        svc.require("sparse").search(req.query, flt, limit=s.fusion_top_n),
    )
    return generation, dense, sparse


@app.post("/search", response_model=RetrievalResponse)
async def search(req: RetrievalRequest, request: Request) -> RetrievalResponse:
    svc = request.app.state.services
    if svc is None:
        raise HTTPException(503, "retrieval dependencies unavailable")
    try:
        generation, dense, sparse = await retrieve_candidates(svc, req)
    except (LookupError, ValueError, Conflict) as exc:
        raise HTTPException(409, "no compatible published index generation") from exc
    payloads = {cid: p for cid, _, p in dense} | {cid: p for cid, _, p in sparse}
    fused = rrf([[c for c, _, _ in dense], [c for c, _, _ in sparse]], k=s.rrf_k)
    if fused is None:
        domain_unavailable()
    candidates = [(cid, payloads[cid][TEXT_FIELD]) for cid, _ in fused[:s.fusion_top_n]]
    try:
        response = await request.app.state.http.post(
            f"{s.reranker_url}/rerank",
            json={"query": req.query, "candidates": [
                {"id": cid, "text": text} for cid, text in candidates], "top_k": req.top_k},
        )
        response.raise_for_status()
        ranked = response.json()["results"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(503, "reranker unavailable") from exc
    return RetrievalResponse(
        hits=[Hit(
            chunk_id=row["id"], score=row["score"], text=payloads[row["id"]][TEXT_FIELD],
            section_path=payloads[row["id"]][SECTION_FIELD], source="reranked",
            citation=Citation(
                study_id=req.study_id, chunk_id=row["id"],
                source_revision=payloads[row["id"]]["source_revision"],
                parser_version=payloads[row["id"]]["parser_version"],
                source_location=payloads[row["id"]]["source_location"],
            ),
        ) for row in ranked],
        trace_id=tracing.CORRELATION_ID.get(), index_generation=generation,
    )

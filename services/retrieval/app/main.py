"""Retrieval orchestration: select one published generation for both stores."""
import asyncio
import logging
import math
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from medw_core import tracing
from medw_core.composition import effective_settings
from medw_core.persistence import Conflict
from medw_core.projections import SECTION_FIELD, TEXT_FIELD
from medw_core.schemas import Citation, DocType, Hit, RetrievalRequest, RetrievalResponse
from medw_core.service import (
    add_platform_routes,
    attach_request_instrumentation,
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
attach_request_instrumentation(app, "retrieval")
add_platform_routes(app, s)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


class StudySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=16000)
    doc_types: list[DocType] | None = None
    section_prefix: str | None = None
    kind: Literal["prose", "table_rows"] | None = None
    top_k: int = Field(default=8, ge=1, le=100)


@app.post("/studies/{study_id}/search", response_model=RetrievalResponse)
async def study_search(study_id: str, body: StudySearchRequest,
                       request: Request) -> RetrievalResponse:
    # NGINX authorizes the path study. A body cannot substitute another study.
    return await search(RetrievalRequest(study_id=study_id, **body.model_dump()), request)


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
    if not 1 <= req.top_k <= 100 or not 1 <= len(req.query) <= 16000:
        raise HTTPException(422, "invalid query length or top_k")
    try:
        generation, dense, sparse = await retrieve_candidates(svc, req)
    except (LookupError, ValueError, Conflict) as exc:
        raise HTTPException(409, "no compatible published index generation") from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("retrieval index query failed")
        raise HTTPException(503, "retrieval indexes unavailable") from exc
    payloads = {cid: p for cid, _, p in dense} | {cid: p for cid, _, p in sparse}
    fused = rrf([[c for c, _, _ in dense], [c for c, _, _ in sparse]], k=s.rrf_k)
    candidates = [(cid, payloads[cid][TEXT_FIELD]) for cid, _ in fused[:s.fusion_top_n]]
    try:
        response = await request.app.state.http.post(
            f"{s.reranker_url}/rerank",
            json={"query": req.query, "candidates": [
                {"id": cid, "text": text} for cid, text in candidates], "top_k": req.top_k},
        )
        response.raise_for_status()
        ranked = response.json()["results"]
        if (not isinstance(ranked, list) or len(ranked) > req.top_k
                or len({row["id"] for row in ranked}) != len(ranked)
                or any(row["id"] not in dict(candidates)
                       or not isinstance(row["score"], (float, int))
                       or not math.isfinite(row["score"]) for row in ranked)):
            raise ValueError("invalid reranker response")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
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

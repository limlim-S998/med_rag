# The retrieval service.
#
# Everything is async because every hop is I/O: embed call, Qdrant, Cognitive
# Search, reranker. There is no CPU work here worth a thread pool. The dense
# and sparse halves run concurrently - they are independent, so serialising
# them just adds their latencies together for no reason.

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from medw_core import azure, tracing
from medw_core.projections import SECTION_FIELD, TEXT_FIELD
from medw_core.schemas import Hit, RetrievalRequest, RetrievalResponse
from medw_core.settings import get_settings

from .fusion import rrf
from .qdrant_repo import QdrantRepo
from .sparse_repo import SparseRepo

s = get_settings()
ctx: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.configure_logging(s.log_level, "retrieval")
    cred = azure.credential()
    ctx["cred"] = cred
    ctx["aoai"] = azure.openai_client(s, cred)
    ctx["qdrant"] = QdrantRepo(s)
    ctx["sparse"] = SparseRepo(azure.search_client(s, cred))
    ctx["http"] = httpx.AsyncClient(timeout=10.0)
    yield
    await ctx["http"].aclose()
    await cred.close()


app = FastAPI(title="retrieval", lifespan=lifespan)


@app.middleware("http")
async def correlate(request: Request, call_next):
    cid = request.headers.get(tracing.HEADER) or tracing.new_id()
    tracing.CORRELATION_ID.set(cid)
    response = await call_next(request)
    response.headers[tracing.HEADER] = cid
    return response


# Liveness = is the process alive. Readiness = can it serve.
# Conflating them means a slow Qdrant restarts your pods in a crash loop
# instead of taking them out of rotation until it recovers.
@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    try:
        await ctx["qdrant"].client.get_collections()
        await ctx["http"].get(f"{s.reranker_url}/healthz")
        return Response(status_code=200)
    except Exception:
        return Response(status_code=503)


@app.post("/search", response_model=RetrievalResponse)
async def search(req: RetrievalRequest) -> RetrievalResponse:
    # One filter object, built once, handed to both halves. Previously each
    # half was passed its own hand-assembled arguments, and the sparse call
    # was missing section_prefix - so a section-scoped query fused scoped
    # dense hits with unscoped sparse ones and silently ranked the wrong rows.
    # Building it here means the two halves cannot be given different things.
    flt = req.to_filter()

    emb = await ctx["aoai"].embeddings.create(
        model=s.embed_deployment,          # deployment name, and it MUST be the
        input=[req.query],                 # same one used at index time
    )
    vector = emb.data[0].embedding

    dense, sparse = await asyncio.gather(
        ctx["qdrant"].search(vector, flt, limit=s.fusion_top_n),
        ctx["sparse"].search(req.query, flt, limit=s.fusion_top_n),
    )

    # Results from the two halves are merged into one dict and then read
    # without knowing which store a given hit came from. That only works
    # because both projections spell the shared fields identically - which is
    # now enforced in medw_core.projections rather than assumed. Reading the
    # field names from there means this service cannot drift from the sinks.
    payloads = {cid: p for cid, _, p in dense} | {cid: p for cid, _, p in sparse}
    fused = rrf([[c for c, _, _ in dense], [c for c, _, _ in sparse]], k=s.rrf_k)
    candidates = [(cid, payloads[cid][TEXT_FIELD]) for cid, _ in fused[:s.fusion_top_n]]

    r = await ctx["http"].post(
        f"{s.reranker_url}/rerank",
        json={"query": req.query, "candidates": [
            {"id": c, "text": t} for c, t in candidates], "top_k": req.top_k},
        headers={tracing.HEADER: tracing.CORRELATION_ID.get()},
    )
    ranked = r.json()["results"]

    return RetrievalResponse(
        hits=[Hit(chunk_id=x["id"], score=x["score"],
                  text=payloads[x["id"]][TEXT_FIELD],
                  section_path=payloads[x["id"]][SECTION_FIELD], source="reranked")
              for x in ranked],
        trace_id=tracing.CORRELATION_ID.get(),
    )

# The retrieval service.
#
# Everything is async because every hop is I/O: embed call, Qdrant, Cognitive
# Search, reranker. There is no CPU work here worth a thread pool. The dense
# and sparse halves run concurrently - they are independent, so serialising
# them just adds their latencies together for no reason.

import asyncio
import dataclasses
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from medw_core import tracing
from medw_core.composition import build
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
    """Wiring comes from the composition root, not from here.

    This used to build a credential, an AOAI client and two repos inline into
    an untyped dict — the same block, slightly different, in four services. Now
    the shared dependencies come from medw_core.composition and this function
    only attaches the two adapters that belong to *this* service.

    The store adapters are attached with dataclasses.replace rather than built
    in the composition root, because they live in this package and medw_core
    must never import service code (tests/test_architecture.py enforces it).
    """
    tracing.configure_logging(s.log_level, "retrieval")
    async with AsyncExitStack() as stack:
        if s.backend == "azure":
            # Built here and passed in, so this process has ONE credential.
            # Letting build() make its own would give two token caches
            # refreshing independently against the same tenant.
            from medw_core import azure
            cred = azure.credential()
            await stack.enter_async_context(cred)
            services = await build(s, stack, credential=cred)
            services = dataclasses.replace(
                services,
                vectors=QdrantRepo(s),
                sparse=SparseRepo(azure.search_client(s, cred)),
            )
        else:
            services = await build(s, stack)
            # Qdrant is the real thing locally too; only the sparse half is
            # substituted, and build() has already wired the in-memory one.
            services = dataclasses.replace(services, vectors=QdrantRepo(s))

        ctx["services"] = services
        ctx["http"] = httpx.AsyncClient(timeout=10.0)
        stack.push_async_callback(ctx["http"].aclose)
        yield


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
        await ctx["services"].require("vectors").client.get_collections()
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

    svc = ctx["services"]
    # Through the port. This handler no longer knows whether it is talking to
    # Azure OpenAI or a hash function, which is the entire point.
    vector = (await svc.require("embedder").embed([req.query]))[0]

    dense, sparse = await asyncio.gather(
        svc.require("vectors").search(vector, flt, limit=s.fusion_top_n),
        svc.require("sparse").search(req.query, flt, limit=s.fusion_top_n),
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

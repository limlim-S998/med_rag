# The gateway. Auth, session, correlation ID minting, fan-out.
#
# It holds no domain logic on purpose. Everything it does is a cross-cutting
# concern that would otherwise be duplicated in four services and drift:
#
#   - Validate the Entra ID token. Once, here. Internal hops trust the network.
#   - Authorise the study. "May this oid see ABC-101" is a lookup in the
#     relational store, not a JWT claim - study access changes daily and a
#     token lives an hour.
#   - Mint the correlation ID. One writer action = one ID = one App Insights
#     trace across gateway -> retrieval -> reranker -> generation, and the same
#     ID lands in the audit row.
#   - Hold the session in Cosmos, so the writer's open study and recent
#     retrieval context survive a pod restart.
#
# This is also the only service exposed through the ingress. Nothing else has
# a public address.

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request, Response

from medw_core import tracing
from medw_core.auth import Principal, current_user
from medw_core.composition import build
from medw_core.settings import get_settings

from .routes import documents, draft, search

s = get_settings()
ctx: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Session store comes from the composition root; the HTTP client does not.

    `sessions` is a port with a Cosmos implementation and an in-memory one, so
    it is wired centrally. The httpx client is not a seam - it is this
    service's own connection pool to the internal services, and there is no
    second implementation anyone would want.
    """
    tracing.configure_logging(s.log_level, "gateway")
    async with AsyncExitStack() as stack:
        ctx["services"] = await build(s, stack)
        # One client, reused. A new AsyncClient per request leaks connections
        # and loses keep-alive to the internal services, which is most of the
        # win. 60s because generation streams and the gateway proxies it.
        ctx["http"] = httpx.AsyncClient(timeout=60.0)
        stack.push_async_callback(ctx["http"].aclose)
        yield


app = FastAPI(title="gateway", lifespan=lifespan)
app.include_router(search.router)
app.include_router(draft.router)
app.include_router(documents.router)


@app.middleware("http")
async def correlate(request: Request, call_next):
    # Minted here and nowhere else. Downstream services accept the header if
    # present and generate one only when called directly (i.e. in dev).
    cid = request.headers.get(tracing.HEADER) or tracing.new_id()
    tracing.CORRELATION_ID.set(cid)
    response = await call_next(request)
    response.headers[tracing.HEADER] = cid
    return response


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    # Will check the dependencies it cannot serve without: Cosmos for
    # sessions, retrieval and generation downstream. Not liveness - a slow
    # Cosmos should take this pod out of rotation, not restart it.
    #
    # 503 until then. Fails closed on purpose: this endpoint returned 200 with
    # every dependency unreachable, because a `...` body returns None and
    # FastAPI renders that as a 200.
    return Response(status_code=503)


@app.get("/me")
async def me(user: Principal = Depends(current_user)) -> dict:
    return {"oid": user.oid, "roles": user.roles}

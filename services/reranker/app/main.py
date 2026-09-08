# The only service that would have model weights in the cluster.
#
# A bi-encoder embeds query and chunk separately and compares vectors - cheap,
# because chunk vectors are precomputed, but the two never see each other. A
# cross-encoder concatenates (query, chunk) and runs them through the model
# together, so attention crosses between them. Much better, far too slow to
# run over a corpus. Hence: fuse to ~30, cross-encode those, keep 5-8.
#
# CPU is fine here. 30 pairs at a few hundred ms sits inside a generation step
# measured in seconds. A GPU node pool for this would be spending money to
# make a non-bottleneck faster.
#
# MODEL HELD BACK. The CrossEncoder load and predict are in holding/ and on
# the implementation/retrieval-slice branch. What stays is the part the
# platform owns: the service shape, the probe semantics, and the fact that
# readiness depends on the weights being resident.

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from pydantic import BaseModel

from medw_core import metrics

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # baked into the image
model = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading takes tens of seconds, which is what the startupProbe in
    # values.yaml is sized for. While a startup probe is failing Kubernetes
    # suppresses liveness - without it the pod is killed mid-load, forever.
    global model
    model = None   # held back: the real body is CrossEncoder(MODEL_ID)
    yield


app = FastAPI(title="reranker", lifespan=lifespan)

# Scrape endpoint and the in-flight gauge. Prometheus is what KEDA reads; the
# same instruments also go to App Insights via metrics.configure(). One set of
# instruments, two readers - see medw_core.metrics.
metrics.configure_prometheus()
app.add_middleware(metrics.InFlightMiddleware, service="reranker")


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    body, content_type = metrics.render_prometheus()
    return Response(content=body, media_type=content_type)


class Candidate(BaseModel):
    id: str
    text: str


class RerankRequest(BaseModel):
    query: str
    candidates: list[Candidate]
    top_k: int = 8


# Liveness is process-up. It must NOT check the model, or a slow load turns
# into a restart loop that guarantees the load never finishes.
@app.get("/healthz")
def healthz() -> Response:
    return Response(status_code=200)


# Readiness is "can it serve", which here means the weights are resident.
# 503 while model is None - an unimplemented probe fails closed.
@app.get("/readyz")
def readyz() -> Response:
    return Response(status_code=200 if model is not None else 503)


@app.post("/rerank")
def rerank(req: RerankRequest):
    # Returns [{"id", "score"}] sorted desc, truncated to top_k.
    ...

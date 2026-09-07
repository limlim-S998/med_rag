# The only model with weights in the cluster.
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

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # baked into the image
model: CrossEncoder | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading takes tens of seconds. It happens here, before the app reports
    # ready, which is what the startupProbe in values.yaml is sized for.
    global model
    model = CrossEncoder(MODEL_ID, max_length=512)
    yield


app = FastAPI(title="reranker", lifespan=lifespan)


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
# These were one endpoint returning {"ok": ...} with a 200 either way - so
# Kubernetes saw a healthy pod that could not answer, and the chart's
# readinessProbe pointed at a path that did not exist at all.
@app.get("/readyz")
def readyz() -> Response:
    return Response(status_code=200 if model is not None else 503)


@app.post("/rerank")
def rerank(req: RerankRequest):
    pairs = [(req.query, c.text) for c in req.candidates]
    scores = model.predict(pairs)
    ranked = sorted(zip(req.candidates, scores, strict=True), key=lambda x: x[1], reverse=True)
    return {"results": [{"id": c.id, "score": float(sc)} for c, sc in ranked[:req.top_k]]}

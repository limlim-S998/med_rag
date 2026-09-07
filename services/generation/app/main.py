# Generation. Streaming, rate-limited, audited.
#
# Three things this service owns that nothing else does:
#
#   1. The token-bucket limiter. AOAI quota is per-deployment tokens-per-minute
#      and it is shared with the ingestion embedding path, so the limiter lives
#      here as a process-local bucket sized from the pod's share of the quota.
#      Per-pod, not global - a distributed limiter would need a shared store on
#      the hot path to solve a problem that replica-count arithmetic solves.
#   2. Streaming. StreamingResponse over the AOAI token stream so the writer
#      sees text appear. A section is thousands of tokens; the wait for a
#      complete response is the difference between a tool people use and one
#      they close.
#   3. The audit write. Every completed section becomes one row in Azure SQL
#      with its versions, its source chunk IDs and its verification verdicts.
#      After the stream finishes, never before - a stream the client abandoned
#      is not a generation that happened.

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

from medw_core import azure, metrics, tracing
from medw_core.rate_limit import TokenBucket
from medw_core.settings import get_settings
from medw_core.sql import access_token_struct, engine

s = get_settings()
ctx: dict = {}

# Per-pod share of the deployment's TPM. maxReplicas in values.yaml and this
# number are the same fact expressed twice - if they disagree, the cluster
# will cheerfully exceed the quota at peak.
POD_TPM = 10_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.configure_logging(s.log_level, "generation")
    if s.appinsights_connection_string:
        metrics.configure(s.appinsights_connection_string, "generation")
    cred = azure.credential()
    ctx["cred"] = cred
    ctx["aoai"] = azure.openai_client(s, cred)
    ctx["bucket"] = TokenBucket(POD_TPM)
    ctx["sql"] = engine(s, await access_token_struct(cred))
    ctx["language"] = azure.language_client(s, cred)   # coded-term verification
    yield
    await ctx["sql"].dispose()
    await cred.close()


app = FastAPI(title="generation", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    # AOAI reachability and a SQL ping. Note the AOAI check must not be a real
    # completion - a readiness probe that spends quota every five seconds is a
    # readiness probe that causes the outage it is watching for.
    ...


@app.post("/draft")
async def draft(req: dict) -> StreamingResponse:
    # 1. table_to_text.render() builds the deterministic numeric spine.
    # 2. The model is asked for connective prose around the fixed slots.
    # 3. verify.py re-extracts every numeral and diffs against the slots.
    # 4. audit.record() writes the row.
    ...

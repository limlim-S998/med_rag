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

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

from medw_core import metrics, tracing
from medw_core.composition import build, readiness
from medw_core.provenance import Provenance
from medw_core.settings import get_settings
from medw_core.sql import access_token_struct, engine

s = get_settings()
ctx: dict = {}

# Per-pod TPM share moved to Settings.pod_tpm, so Helm can set it and the
# composition root can hand one bucket to every AOAI adapter in the process.


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Chat client and entity extractor come from the composition root.

    The AOAI token bucket moved there too: it is per-deployment quota shared by
    the embedder and the chat client, so one bucket per process is correct and
    two would each believe they had the whole allowance.

    The SQL engine is still built lazily (see sql_engine below) - it is the one
    dependency whose construction reaches the network.
    """
    tracing.configure_logging(s.log_level, "generation")
    # Captured once. A per-request read would report the config at write time,
    # which during a rollout differs from what produced the text.
    ctx["provenance"] = Provenance.from_settings(s)
    if s.appinsights_connection_string:
        metrics.configure(s.appinsights_connection_string, "generation")
    async with AsyncExitStack() as stack:
        if s.backend == "azure":
            from medw_core import azure
            cred = azure.credential()
            await stack.enter_async_context(cred)
            ctx["cred"] = cred
            ctx["services"] = await build(s, stack, credential=cred)
        else:
            ctx["cred"] = None
            ctx["services"] = await build(s, stack)
        ctx["sql"] = None
        stack.push_async_callback(_dispose_sql)
        yield


async def _dispose_sql() -> None:
    if ctx.get("sql") is not None:
        await ctx["sql"].dispose()


async def sql_engine():
    """Built on first use, not at startup.

    This used to be `engine(s, await access_token_struct(cred))` inside
    lifespan, and that call reaches out to AAD for a token. The consequence was
    that the process could not start at all without a live Azure connection:
    locally it died with a credential-chain error, and in the cluster a
    transient AAD blip during a rollout would crash-loop new pods instead of
    letting them start and report unready.

    Which is the same liveness-versus-readiness distinction this repo argues
    everywhere else, applied to startup. A dependency being unreachable is a
    readiness problem; only the process being broken is a liveness problem, and
    failing to boot turns the first into the second.
    """
    if ctx.get("cred") is None:
        raise RuntimeError(
            "the audit sink needs an Azure credential; MEDW_BACKEND=local has none. "
            "Under the local backend the audit trail is medw_core.local.audit."
        )
    if ctx["sql"] is None:
        # Tokens expire, so this is also where a refresh would go; the engine
        # is pooled with pool_pre_ping, which stops a pooled connection
        # outliving the token that opened it.
        ctx["sql"] = engine(s, await access_token_struct(ctx["cred"]))
    return ctx["sql"]


app = FastAPI(title="generation", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    # the chat client is the one dependency a draft cannot proceed without.
    ready, reason = readiness(ctx["services"], ("chat",))
    return Response(status_code=200 if ready else 503,
                    headers={"x-readiness-reason": reason})


@app.post("/draft")
async def draft(req: dict) -> StreamingResponse:
    # 1. table_to_text.render() builds the deterministic numeric spine.
    # 2. The model is asked for connective prose around the fixed slots.
    # 3. verify.py re-extracts every numeral and diffs against the slots.
    # 4. audit.record() writes the row.
    ...

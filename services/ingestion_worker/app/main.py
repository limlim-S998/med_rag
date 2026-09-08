# The synchronous single-document path.
#
# Why it exists next to the Airflow DAG: a writer who has just uploaded a
# protocol amendment wants to query it now, not on the next scheduled run.
# Airflow's minimum useful latency is scheduler-bound and its failure mode is
# "check the UI"; a writer needs an HTTP status.
#
# Both entry points call the SAME functions in pipelines/ - the DAG expands
# them across tasks, this service awaits them in one request. If the two paths
# had separate implementations they would diverge, and then a document would
# be chunked differently depending on how it happened to arrive.

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Response

from medw_core import tracing
from medw_core.composition import build, readiness
from medw_core.settings import get_settings

from .jobs import run_ingest

s = get_settings()
ctx: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The widest dependency set in the system, wired in one place.

    This service reads and writes Blob, calls Document Intelligence, Language
    and AOAI embeddings, and writes Qdrant, Cognitive Search, Cosmos and the
    SQL document registry. That breadth is exactly why the ad-hoc dict was
    worst here - twelve assignments with no statement of what was required
    versus merely available.
    """
    tracing.configure_logging(s.log_level, "ingestion-worker")
    async with AsyncExitStack() as stack:
        if s.backend == "azure":
            from medw_core import azure
            cred = azure.credential()
            await stack.enter_async_context(cred)
            ctx["services"] = await build(s, stack, credential=cred)
            # Service-owned adapters: the extraction clients and the two
            # sinks are this service's business, not shared vocabulary.
            ctx["docintel"] = azure.docintel_client(s, cred)
            ctx["search"] = azure.search_client(s, cred)
            ctx["blob"] = azure.blob_client(s, cred)
        else:
            ctx["services"] = await build(s, stack)
        yield


app = FastAPI(title="ingestion-worker", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    # job state and the document registry; the extraction clients are checked when a job actually runs.
    ready, reason = readiness(ctx["services"], ("jobs", "documents"))
    return Response(status_code=200 if ready else 503,
                    headers={"x-readiness-reason": reason})


@app.post("/ingest", status_code=202)
async def ingest(study_id: str, doc_id: str, blob_path: str, bg: BackgroundTasks):
    # 202 with a job ID, not a blocking call. Document Intelligence alone can
    # take minutes on a large package; an HTTP request held open that long is
    # a request that dies to an ingress timeout and leaves state half-written.
    job = await ctx["services"].require("jobs").create(study_id, doc_id)
    bg.add_task(run_ingest, ctx, job)
    return {"job_id": job["id"], "state": job["state"]}

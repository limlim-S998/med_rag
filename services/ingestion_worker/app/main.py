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

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Response

from medw_core import azure, tracing
from medw_core.cosmos import DocumentRepo, containers
from medw_core.settings import get_settings

from .jobs import JobStore, run_ingest

s = get_settings()
ctx: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.configure_logging(s.log_level, "ingestion-worker")
    cred = azure.credential()
    ctx["cred"] = cred
    ctx["aoai"] = azure.openai_client(s, cred)          # embeddings
    ctx["blob"] = azure.blob_client(s, cred)
    ctx["docintel"] = azure.docintel_client(s, cred)
    ctx["language"] = azure.language_client(s, cred)
    ctx["search"] = azure.search_client(s, cred)        # sparse sink
    ctx["cosmos"] = azure.cosmos_client(s, cred)
    c = containers(ctx["cosmos"], s)
    ctx["documents"] = DocumentRepo(c["documents"])
    ctx["jobs"] = JobStore(c["jobs"])
    yield
    await ctx["cosmos"].close()
    await cred.close()


app = FastAPI(title="ingestion-worker", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz() -> Response:
    # Will check Blob, Cosmos, Qdrant and Document Intelligence - this service
    # has the widest dependency set in the system, so it is the one most
    # likely to be up and unable to work.
    #
    # 503 until then: an unimplemented readiness probe has to fail closed.
    return Response(status_code=503)


@app.post("/ingest", status_code=202)
async def ingest(study_id: str, doc_id: str, blob_path: str, bg: BackgroundTasks):
    # 202 with a job ID, not a blocking call. Document Intelligence alone can
    # take minutes on a large package; an HTTP request held open that long is
    # a request that dies to an ingress timeout and leaves state half-written.
    job = await ctx["jobs"].create(study_id, doc_id)
    bg.add_task(run_ingest, ctx, job)
    return {"job_id": job["id"], "state": job["state"]}

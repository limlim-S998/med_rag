# Ingestion as an explicit state machine in Cosmos.
#
# Explicit because "it failed somewhere in ingestion" is not an operable
# message. Each transition is a small write to the jobs container, partitioned
# by /study_id, so the status endpoint is a single-partition point read and
# the writer sees which stage is running.
#
# Every stage is idempotent, so recovery is "re-run from the last completed
# state" rather than "clean up and start over". That property is bought by the
# deterministic IDs, and it is the reason the whole pipeline can be retried
# blind after a parser fix.

from enum import StrEnum


class State(StrEnum):
    queued = "queued"
    extracting = "extracting"      # Document Intelligence -> Blob parsed/
    classifying = "classifying"    # sklearn doc-type classifier
    chunking = "chunking"          # parsers/table.py + parsers/chunker.py
    annotating = "annotating"      # Azure Language clinical NER
    embedding = "embedding"        # AOAI, rate-limited
    indexing = "indexing"          # Qdrant upsert + Cognitive Search upload
    done = "done"
    failed = "failed"


# Terminal-failure states get the exception text and the stage. Retries are
# bounded and per-stage: re-running `embedding` after a 429 is free, but
# re-running `extracting` costs real money per page, which is exactly why the
# layout JSON is cached in Blob before that transition is recorded.
MAX_RETRIES = {State.embedding: 5, State.indexing: 3, State.extracting: 1}


class JobStore:
    def __init__(self, container):
        self.c = container

    async def create(self, study_id: str, doc_id: str) -> dict:
        ...

    async def advance(self, job: dict, state: State) -> dict:
        ...

    async def fail(self, job: dict, state: State, error: str) -> dict:
        ...


async def run_ingest(ctx: dict, job: dict) -> None:
    # The single-document path, straight-line. The DAG runs these same calls
    # as separate tasks so Airflow can retry and parallelise them; here they
    # are awaited in order because there is one document and a writer waiting.
    ...

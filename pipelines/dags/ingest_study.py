# Bulk ingestion. Airflow owns the batch path; the synchronous FastAPI
# ingestion worker owns the single-document path so a writer can query a doc
# they are actively editing. Same functions underneath, two entry points.

from datetime import UTC, datetime

from airflow.decorators import dag, task

from medw_core.settings import get_settings


@dag(
    dag_id="ingest_study",
    schedule=None,                       # triggered per study with a conf payload
    start_date=datetime(2025, 4, 1, tzinfo=UTC),   # Airflow compares against aware now()
    catchup=False,
    max_active_runs=1,                   # one study at a time; AOAI quota is shared
    tags=["ingestion"],
)
def ingest_study():

    @task
    def list_blobs(study_id: str) -> list[str]:
        ...

    @task
    def extract(blob_path: str) -> dict:
        # Document Intelligence layout model -> custom parsers
        ...

    @task
    def classify(doc: dict) -> dict:
        # sklearn doc-type classifier, loaded from the Azure ML registry by
        # pinned version. Fast, deterministic, auditable.
        ...

    @task
    def chunk(doc: dict) -> list[dict]:
        ...

    @task
    def embed_and_upsert(chunks: list[dict]) -> int:
        # Batched embeddings, then Qdrant upsert + Cognitive Search index.
        # Idempotent because the IDs are deterministic - this is what lets you
        # re-run the whole DAG after a parser fix instead of dropping the
        # collection.
        ...

    s = get_settings()  # noqa: F841
    docs = list_blobs.override(task_id="list")("{{ dag_run.conf['study_id'] }}")
    embed_and_upsert(chunk(classify(extract.expand(blob_path=docs))))


ingest_study()

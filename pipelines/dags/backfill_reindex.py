# The DAG you run when something upstream of the index changed.
#
# Two triggers, and they cost very differently:
#
#   parser bump  - PARSER_VERSION changes, so every chunk ID changes. Re-run
#                  OUR parsers over the layout JSON already cached in Blob.
#                  No Document Intelligence spend, no re-embedding of prose
#                  whose text did not change.
#   embed bump   - the embedding deployment changed, so embed_version changes,
#                  so the collection name changes. Build a NEW collection,
#                  eval it against the golden set, flip the alias. Never
#                  mutate in place: the two vector spaces are not comparable
#                  and a half-migrated collection returns plausible garbage.
#
# The eval gate in the middle is the part worth pointing at. A backfill that
# does not measure recall before cutting over is a deployment with no test.

from datetime import UTC, datetime

from airflow.decorators import dag, task


@dag(
    dag_id="backfill_reindex",
    schedule=None,
    start_date=datetime(2025, 4, 1, tzinfo=UTC),   # Airflow compares against aware now()
    catchup=False,
    max_active_runs=1,        # AOAI embedding quota is shared with live traffic
    tags=["ingestion", "backfill"],
)
def backfill_reindex():

    @task
    def studies_needing_backfill() -> list[str]:
        # SELECT DISTINCT study_id FROM audit.index_event
        # WHERE parser_version <> :current OR embed_version <> :current
        # The audit table answers this without scanning either index.
        ...

    @task
    def rechunk_from_cached_layout(study_id: str) -> dict:
        # Reads parsed/{study}/{doc}/{parser_version}/layout.json from Blob.
        ...

    @task
    def build_shadow_collection(spec: dict) -> str:
        ...

    @task
    def eval_gate(collection: str) -> bool:
        # evals/run_retrieval_eval.py against the shadow collection. Fails the
        # DAG if recall@3 regresses beyond tolerance - the alias never moves.
        ...

    @task
    def flip_alias(collection: str) -> None:
        ...

    s = studies_needing_backfill()
    flip_alias(build_shadow_collection(rechunk_from_cached_layout.expand(study_id=s)))


backfill_reindex()

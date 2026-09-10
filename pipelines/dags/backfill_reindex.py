# Backfill DAG boundary, held back with clinical source planning and evaluation.
# Every task below fails explicitly until those callbacks are supplied. This
# graph is not a functioning or scheduled clinical backfill pipeline.
#
# The retained platform implementation is medw_core.indexing.publish_generation:
# stage both stores, inspect their content, require evaluation, retain evidence,
# then conditionally publish ONE shared manifest. Readers never cut over through
# a Qdrant-only alias. The legacy task name flip_alias remains for continuity;
# its future implementation must publish the validated shared manifest.
#
# Parser/source changes create new immutable chunk identities; embedding changes
# create a new compatible generation. Reuse of cached extraction/embeddings is
# a future ingestion policy, not a behavior proved by this held-back DAG.

from datetime import UTC, datetime

from airflow.decorators import dag, task


@dag(
    dag_id="backfill_reindex",
    schedule=None,
    start_date=datetime(2025, 4, 1, tzinfo=UTC),   # Airflow compares against aware now()
    catchup=False,
    max_active_runs=1,        # Bound future backfill load on the embedding deployment.
    tags=["ingestion", "backfill"],
)
def backfill_reindex():

    @task
    def studies_needing_backfill() -> list[str]:
        # Compare active IndexRegistry manifests with the desired release.
        # Historical audit events alone do not identify what is active now.
        raise NotImplementedError("backfill source discovery is held back with ingestion")

    @task
    def rechunk_from_cached_layout(study_id: str) -> dict:
        # Resolve immutable source/parser artifact references from retained evidence.
        raise NotImplementedError("clinical rechunking is held back")

    @task
    def build_shadow_collection(spec: dict) -> str:
        raise NotImplementedError("build staged manifests with publish_generation's sink contract")

    @task
    def eval_gate(collection: str) -> str:
        # The clinical evaluator must reject regressions before publication.
        # No clinical retrieval-quality result is claimed by scaffold tests.
        raise NotImplementedError("medical retrieval evaluation is held back; no publication")

    @task
    def flip_alias(collection: str) -> None:
        raise NotImplementedError("publish the evaluated manifest through IndexRegistry.activate")

    s = studies_needing_backfill()
    specs = rechunk_from_cached_layout.expand(study_id=s)
    collections = build_shadow_collection.expand(spec=specs)
    evaluated = eval_gate.expand(collection=collections)
    flip_alias.expand(collection=evaluated)


backfill_reindex()

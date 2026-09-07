# Azure Cosmos DB - the semi-structured, high-churn half of the state.
#
# The split that gets asked about (see docs/adr/0004): Cosmos holds things
# whose shape changes and whose write rate is high - document metadata,
# ingestion job state, writer sessions. Azure SQL holds things that want joins
# and constraints - the audit trail, the study/document registry, the
# regulatory reporting queries. It is not one store used twice; it is two
# shapes of data.
#
# The only Cosmos decision that is hard to undo is the partition key. Pick it
# so that (a) reads are single-partition and (b) no single partition takes a
# disproportionate share of writes. Here every access pattern is scoped to a
# study, so /study_id is the key almost everywhere - a query for one study's
# documents never fans out, and studies arrive independently so writes spread.
#
# Sessions are the exception: they are keyed by /user_id, because the read is
# always "this writer's session", never "this study's sessions".

from typing import Any

from azure.cosmos.aio import ContainerProxy, CosmosClient

from medw_core.settings import Settings

# Container -> partition key. Mirrored in db/cosmos/containers.json, which is
# what bootstrap.sh actually creates. Keep the two in step.
CONTAINERS = {
    "documents": "/study_id",     # one item per ingested source document
    "jobs": "/study_id",          # ingestion job state machine
    "sessions": "/user_id",       # writer session: open study, draft context
    "generations": "/study_id",   # what the model produced, before it is audited
}


def containers(client: CosmosClient, s: Settings) -> dict[str, ContainerProxy]:
    db = client.get_database_client(s.cosmos_database)
    return {name: db.get_container_client(name) for name in CONTAINERS}


class DocumentRepo:
    """Metadata for one source document. The bytes stay in Blob; this is the
    row that says where they are, what type they were classified as, and which
    parser version last touched them."""

    def __init__(self, container: ContainerProxy):
        self.c = container

    async def upsert(self, doc: dict[str, Any]) -> dict[str, Any]:
        # id is deterministic (study + blob path + parser version), so a DAG
        # re-run overwrites rather than duplicates - same property the chunk
        # IDs have, for the same reason.
        return await self.c.upsert_item(doc)

    async def by_study(self, study_id: str) -> list[dict[str, Any]]:
        # partition_key set => single-partition query. Without it this is a
        # cross-partition fan-out that gets slower as studies accumulate.
        q = "SELECT * FROM c WHERE c.study_id = @s ORDER BY c.ingested_at DESC"
        it = self.c.query_items(
            query=q,
            parameters=[{"name": "@s", "value": study_id}],
            partition_key=study_id,
        )
        return [item async for item in it]


class SessionRepo:
    """Writer session. Short-lived, and the container carries a TTL so expiry
    is the database's job rather than a cleanup cron nobody maintains."""

    def __init__(self, container: ContainerProxy):
        self.c = container

    async def get(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        ...

    async def put(self, session: dict[str, Any]) -> None:
        ...

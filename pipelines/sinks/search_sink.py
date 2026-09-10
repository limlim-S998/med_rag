"""Stage complete Search documents under a generation-specific key."""

from azure.search.documents.aio import SearchClient

from medw_core.indexing import IndexReceipt, payload_digest
from medw_core.persistence import Conflict
from medw_core.projections import to_generation_search_document
from medw_core.schemas import Chunk, IndexGeneration

DOC_BATCH = 500


def generation_filter(generation: IndexGeneration) -> str:
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    return (f"study_id eq {quote(generation.study_id)} and "
            f"index_generation eq {quote(generation.sparse_generation)}")


class SearchGenerationSink:
    def __init__(self, client: SearchClient):
        self.client = client

    async def stage(self, generation: IndexGeneration, chunks: list[Chunk],
                    vectors: list[list[float]]) -> IndexReceipt:
        docs = [to_generation_search_document(c, generation) for c in chunks]
        for offset in range(0, len(docs), DOC_BATCH):
            results = await self.client.upload_documents(documents=docs[offset:offset + DOC_BATCH])
            if len(results) != len(docs[offset:offset + DOC_BATCH]) or any(
                    not r.succeeded for r in results):
                raise Conflict("Search rejected one or more staged documents")
        # Visibility may lag successful ingestion. A failed inspection blocks
        # publication; the idempotent staging job can be retried after visibility catches up.
        return await self.inspect(generation)

    async def inspect(self, generation: IndexGeneration) -> IndexReceipt:
        rows = await self.client.search(search_text="*", filter=generation_filter(generation))
        chunks = []
        async for row in rows:
            chunk = Chunk.model_validate_json(row["chunk_json"])
            expected = to_generation_search_document(chunk, generation)
            if any(row.get(key) != value for key, value in expected.items()):
                raise Conflict("Search document payload differs from its retained chunk")
            chunks.append(chunk)
        return IndexReceipt(generation.generation_id, len(chunks), payload_digest(chunks))


async def index_chunks(*args, **kwargs) -> int:
    raise RuntimeError("unscoped writes disabled; use SearchGenerationSink and publish_generation")


async def delete_study(client: SearchClient, study_id: str) -> None:
    quote = "'" + study_id.replace("'", "''") + "'"
    rows = await client.search(search_text="*", filter=f"study_id eq {quote}", select=["chunk_id"])
    batch = []
    async for row in rows:
        batch.append({"chunk_id": row["chunk_id"]})
        if len(batch) == DOC_BATCH:
            results = await client.delete_documents(batch)
            if any(not r.succeeded for r in results):
                raise Conflict("Search study deletion partially failed")
            batch = []
    if batch:
        results = await client.delete_documents(batch)
        if any(not r.succeeded for r in results):
            raise Conflict("Search study deletion partially failed")

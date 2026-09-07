# The Cognitive Search write path. Same chunks, same IDs, different store.
#
# Two indexes must not drift. They share the deterministic chunk ID, so the
# reconciliation check is a set difference on IDs between Qdrant and Search
# for a given study - cheap, and it runs nightly. Drift means the sparse half
# silently stops returning something the dense half still finds, which shows
# up as a quiet recall drop rather than an error.
#
# Note this is `mergeOrUpload`, not `upload`: same idempotency property as the
# Qdrant upsert, so both sinks can be re-run over the same DAG output.

from azure.search.documents.aio import SearchClient

from medw_core.projections import to_search_action
from medw_core.schemas import Chunk

DOC_BATCH = 1000   # the service caps a batch at 1000 documents / 16MB


async def index_chunks(client: SearchClient, chunks: list[Chunk],
                       coded_terms: dict[str, list[str]] | None = None) -> int:
    # Document shape is medw_core.projections.to_search_action, which is also
    # what builds the Qdrant payload's shared half. The two used to be written
    # out separately here and in qdrant_sink.py.
    terms = coded_terms or {}
    docs = [to_search_action(c, terms.get(c.id)) for c in chunks]
    for i in range(0, len(docs), DOC_BATCH):
        await client.upload_documents(documents=docs[i:i + DOC_BATCH])
    return len(docs)


async def delete_study(client: SearchClient, study_id: str) -> None:
    # Client teardown. Cheap in Qdrant (drop the collection), expensive here:
    # a shared index means deleting by filtered query, in batches, by key.
    # That asymmetry is the cost of the one-index-for-all-studies choice, and
    # it was the right trade because teardown is rare and cost is monthly.
    ...

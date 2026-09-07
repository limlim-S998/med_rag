# The Qdrant write path. The read path is services/retrieval/app/qdrant_repo.py
# and they deliberately live apart: the service has no write credential on the
# vector store, so a bug in a request handler cannot corrupt an index.
#
# Idempotency is the whole design. Point IDs are a pure function of (study,
# doc, section path, ordinal, parser version), so an upsert of the same chunk
# overwrites in place. That is what makes re-running a DAG safe, which matters
# because a parser fix means backfilling every study - and we did that more
# than once.

from qdrant_client import AsyncQdrantClient, models

from medw_core.ids import collection_name
from medw_core.projections import to_qdrant_payload
from medw_core.schemas import Chunk
from medw_core.settings import Settings

UPSERT_BATCH = 256   # points per request; larger batches stall the gRPC stream


async def upsert_chunks(client: AsyncQdrantClient, s: Settings,
                        study_id: str, chunks: list[Chunk],
                        vectors: list[list[float]],
                        coded_terms: dict[str, list[str]] | None = None) -> int:
    # The payload shape is medw_core.projections.to_qdrant_payload and nothing
    # else. It used to be assembled here, and the Search document was assembled
    # separately in search_sink.py, with nothing checking that the fields the
    # retrieval service reads off both were spelled the same way.
    name = collection_name(study_id, s.embed_version)
    terms = coded_terms or {}
    points = [
        models.PointStruct(
            id=c.id,
            vector=v,
            payload=to_qdrant_payload(c, terms.get(c.id)),
        )
        for c, v in zip(chunks, vectors, strict=True)
    ]
    for i in range(0, len(points), UPSERT_BATCH):
        await client.upsert(collection_name=name, points=points[i:i + UPSERT_BATCH], wait=False)
    return len(points)


async def swap_alias(client: AsyncQdrantClient, study_id: str,
                     old_version: str, new_version: str) -> None:
    # The embedding-change path. You never mutate a collection in place: build
    # the new one alongside under its own versioned name, verify recall against
    # the golden set, then move the alias in one atomic operation. Rollback is
    # moving it back, which is instant, and the old collection is still there.
    ...

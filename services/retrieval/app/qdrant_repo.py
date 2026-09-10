# Qdrant, operationally.
#
# Three decisions worth being able to defend:
#  1. One collection per study. Re-indexing one study does not touch another,
#     and client teardown is a delete. Costs you cross-study search, which
#     nobody asked for.
#  2. Payload indexes on the fields you filter by. Qdrant filters PRE-ANN:
#     the filter constrains the graph traversal rather than discarding results
#     afterwards. Without a payload index that path degrades badly.
#  3. Embedding version in the collection name (see medw_core.ids), so a model
#     change means build-new + alias flip, not a mutate-in-place.

from qdrant_client import AsyncQdrantClient, models

from medw_core.indexing import selected_generation
from medw_core.ports import SearchResult
from medw_core.schemas import RetrievalFilter
from medw_core.settings import Settings


class QdrantRepo:
    """Satisfies medw_core.ports.VectorIndex by shape - no inheritance.

    The port takes a RetrievalFilter; translating it into Qdrant conditions is
    this class's job and nobody else's. That is the boundary: the service asks
    for a filter, the repo knows what Qdrant calls it.
    """

    def __init__(self, s: Settings):
        self.s = s
        self.client = AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None)

    async def ensure_collection(self, study_id: str) -> str:
        raise RuntimeError("retrieval is read-only; use QdrantGenerationSink for collection creation")

    def _conditions(self, flt: RetrievalFilter) -> list[models.Condition]:
        # The Qdrant dialect of RetrievalFilter. Every field the filter grows
        # must be handled here or it is silently ignored - which is exactly
        # how the sparse half ended up dropping section_prefix.
        #
        # study_id is not a condition: it selects the collection. One
        # collection per study means the scoping is structural rather than a
        # filter that could be forgotten.
        must: list[models.Condition] = []
        if doc_types := flt.doc_type_values():
            must.append(models.FieldCondition(
                key="doc_type", match=models.MatchAny(any=doc_types)))
        if flt.section_prefix:
            must.append(models.FieldCondition(
                key="section_prefixes", match=models.MatchValue(value=flt.section_prefix)))
        if flt.kind:
            must.append(models.FieldCondition(
                key="kind", match=models.MatchValue(value=flt.kind)))
        return must

    async def search(self, vector: list[float], flt: RetrievalFilter, *,
                     limit: int) -> list[SearchResult]:
        must = self._conditions(flt)
        generation = selected_generation(flt)
        if len(vector) != generation.dimensions:
            raise ValueError("query vector dimensions differ from the selected index")
        res = await self.client.query_points(
            collection_name=generation.dense_collection,
            query=vector,
            limit=limit,
            query_filter=models.Filter(must=must) if must else None,
            search_params=models.SearchParams(hnsw_ef=self.s.search_ef),
            with_payload=True,
        )
        return [(str(p.id), p.score, p.payload or {}) for p in res.points]

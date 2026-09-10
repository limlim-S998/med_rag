"""Durable synthetic index storage; BM25 remains a local behavior double."""

from medw_core.indexing import IndexReceipt, payload_digest, selected_generation
from medw_core.local.stores import InMemorySparseIndex
from medw_core.persistence import Conflict, StateStore
from medw_core.schemas import Chunk, IndexGeneration, RetrievalFilter


class SQLiteGenerationSink:
    def __init__(self, state: StateStore, role: str = "sparse"):
        if role not in ("dense", "sparse"):
            raise ValueError("index role must be dense or sparse")
        self.state, self.kind = state, f"local_{role}_generation"

    async def stage(self, generation: IndexGeneration, chunks: list[Chunk],
                    vectors: list[list[float]]) -> IndexReceipt:
        body = {"chunks": [c.model_dump() for c in chunks], "vectors": vectors}
        try:
            await self.state.put(self.kind, generation.study_id, generation.generation_id,
                                 body, expected_revision=None)
        except Conflict:
            previous = await self.state.get(self.kind, generation.study_id, generation.generation_id)
            if not previous or previous.value != body:
                raise Conflict("staged generation is immutable") from None
        return await self.inspect(generation)

    async def inspect(self, generation: IndexGeneration) -> IndexReceipt:
        row = await self.state.get(self.kind, generation.study_id, generation.generation_id)
        if not row:
            raise LookupError("generation is absent from local index storage")
        chunks = [Chunk.model_validate(c) for c in row.value["chunks"]]
        return IndexReceipt(generation.generation_id, len(chunks), payload_digest(chunks))


class DurableSparseIndex:
    def __init__(self, state: StateStore):
        self.state = state

    async def search(self, query: str, flt: RetrievalFilter, *, limit: int):
        generation = selected_generation(flt)
        row = await self.state.get("local_sparse_generation", generation.study_id,
                                    generation.generation_id)
        if not row:
            raise LookupError("selected sparse generation is not available")
        temporary = InMemorySparseIndex()
        await temporary.index([Chunk.model_validate(c) for c in row.value["chunks"]])
        return await temporary.search(query, flt, limit=limit)

    async def index(self, chunks: list[Chunk]) -> int:
        raise RuntimeError("retrieval cannot write an index; use SQLiteGenerationSink")

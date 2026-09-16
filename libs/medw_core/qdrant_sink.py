"""Write and read back staged dense generations before publication."""

from qdrant_client import AsyncQdrantClient, models

from medw_core.indexing import IndexReceipt, payload_digest
from medw_core.persistence import Conflict
from medw_core.projections import from_qdrant_payload, to_qdrant_payload
from medw_core.schemas import Chunk, IndexGeneration

UPSERT_BATCH = 256


class QdrantGenerationSink:
    def __init__(self, client: AsyncQdrantClient, *, replication_factor: int = 1,
                 write_consistency_factor: int = 1, shard_number: int = 1):
        self.client = client
        self.replication_factor = replication_factor
        self.write_consistency_factor = write_consistency_factor
        self.shard_number = shard_number

    async def stage(self, generation: IndexGeneration, chunks: list[Chunk],
                    vectors: list[list[float]]) -> IndexReceipt:
        if not await self.client.collection_exists(generation.dense_collection):
            await self.client.create_collection(
                collection_name=generation.dense_collection,
                vectors_config=models.VectorParams(size=generation.dimensions,
                                                    distance=models.Distance.COSINE),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128),
                replication_factor=self.replication_factor,
                write_consistency_factor=self.write_consistency_factor,
                shard_number=self.shard_number)
            for field in ("study_id", "doc_type", "section_path", "section_prefixes", "kind"):
                await self.client.create_payload_index(
                    generation.dense_collection, field, models.PayloadSchemaType.KEYWORD, wait=True)
        points = [models.PointStruct(id=c.id, vector=v, payload=to_qdrant_payload(c))
                  for c, v in zip(chunks, vectors, strict=True)]
        for offset in range(0, len(points), UPSERT_BATCH):
            await self.client.upsert(generation.dense_collection,
                                     points=points[offset:offset + UPSERT_BATCH], wait=True)
        return await self.inspect(generation)

    async def inspect(self, generation: IndexGeneration) -> IndexReceipt:
        chunks = []
        offset = None
        while True:
            rows, offset = await self.client.scroll(
                generation.dense_collection, limit=256, offset=offset, with_payload=True,
                with_vectors=False)
            for point in rows:
                payload = point.payload or {}
                chunk = from_qdrant_payload(str(point.id), payload)
                if any(payload.get(k) != v for k, v in to_qdrant_payload(chunk).items()):
                    raise Conflict("dense payload differs from its retained chunk")
                chunks.append(chunk)
            if offset is None:
                break
        return IndexReceipt(generation.generation_id, len(chunks), payload_digest(chunks))


async def upsert_chunks(*args, **kwargs) -> int:
    raise RuntimeError("unscoped writes disabled; use QdrantGenerationSink and publish_generation")


async def swap_alias(*args, **kwargs) -> None:
    raise RuntimeError("alias-only publication disabled; publish the shared IndexRegistry manifest")

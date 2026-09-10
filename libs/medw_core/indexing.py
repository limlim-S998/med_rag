"""Publish a coherent pair of staged indexes using one conditional pointer.

There is no cross-store transaction. Readers select an immutable manifest once.
Writers finish and verify both stores before atomically changing that pointer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from medw_core.persistence import Conflict, StateStore
from medw_core.schemas import Chunk, IndexGeneration, RetrievalFilter


def make_generation(study_id: str, chunks: list[Chunk], *, parser_version: str,
                    embed_version: str, embed_deployment: str,
                    embed_model_version: str, dimensions: int,
                    embed_model_name: str = "unknown") -> IndexGeneration:
    generation_id = uuid.uuid4().hex
    study_key = hashlib.sha256(study_id.encode()).hexdigest()[:16]
    return IndexGeneration(
        generation_id=generation_id, study_id=study_id,
        dense_collection=f"csr_{study_key}_{generation_id}", sparse_generation=generation_id,
        parser_version=parser_version, embed_version=embed_version,
        embed_deployment=embed_deployment, embed_model_version=embed_model_version,
        embed_model_name=embed_model_name,
        dimensions=dimensions, payload_sha256=payload_digest(chunks), chunk_count=len(chunks))


def payload_digest(chunks: list[Chunk]) -> str:
    payload = [c.model_dump(mode="json") for c in sorted(chunks, key=lambda c: c.id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) \
        .hexdigest()


@dataclass(frozen=True)
class IndexReceipt:
    generation_id: str
    count: int
    payload_sha256: str


class GenerationSink(Protocol):
    async def stage(self, generation: IndexGeneration, chunks: list[Chunk],
                    vectors: list[list[float]]) -> IndexReceipt: ...
    async def inspect(self, generation: IndexGeneration) -> IndexReceipt: ...


class IndexRegistry:
    def __init__(self, state: StateStore):
        self.state = state

    async def active(self, study_id: str) -> tuple[IndexGeneration | None, str | None]:
        row = await self.state.get("active_index", study_id, "active")
        if not row:
            return None, None
        return IndexGeneration.model_validate(row.value["generation"]), row.revision

    async def select(self, study_id: str, *, embed_version: str, embed_deployment: str,
                     dimensions: int, embed_model_version: str = "",
                     embed_model_name: str = "unknown") -> IndexGeneration:
        generation, _ = await self.active(study_id)
        if generation is None:
            raise LookupError(f"no published index for {study_id}")
        if (generation.embed_version, generation.embed_deployment, generation.dimensions,
                generation.embed_model_version, generation.embed_model_name) != (
                embed_version, embed_deployment, dimensions, embed_model_version, embed_model_name):
            raise Conflict("reader embedding identity is incompatible with the selected index")
        return generation

    async def register_validated(self, generation: IndexGeneration) -> None:
        body = generation.model_dump()
        try:
            await self.state.put("validated_index", generation.study_id, generation.generation_id,
                                 body, expected_revision=None)
        except Conflict:
            existing = await self.state.get("validated_index", generation.study_id,
                                            generation.generation_id)
            if not existing or existing.value != body:
                raise Conflict("generation identity is immutable") from None

    async def activate(self, generation: IndexGeneration, *,
                       expected_revision: str | None) -> str:
        validated = await self.state.get("validated_index", generation.study_id,
                                         generation.generation_id)
        if not validated or validated.value != generation.model_dump():
            raise Conflict("only validated generations can be published")
        row = await self.state.put("active_index", generation.study_id, "active",
                                   {"generation": generation.model_dump()},
                                   expected_revision=expected_revision)
        return row.revision

    async def generations(self, study_id: str) -> list[IndexGeneration]:
        return [IndexGeneration.model_validate(r.value)
                for r in await self.state.list("validated_index", study_id)]

    async def rollback(self, study_id: str, target: str, *, expected_revision: str,
                       dense: GenerationSink, sparse: GenerationSink,
                       embed_version: str, embed_deployment: str, dimensions: int,
                       embed_model_version: str = "", embed_model_name: str = "unknown") -> str:
        record = await self.state.get("validated_index", study_id, target)
        if not record:
            raise Conflict("rollback target was never validated")
        generation = IndexGeneration.model_validate(record.value)
        if (generation.embed_version, generation.embed_deployment, generation.dimensions,
                generation.embed_model_version, generation.embed_model_name) != (
                embed_version, embed_deployment, dimensions, embed_model_version, embed_model_name):
            raise Conflict("rollback requires readers compatible with its embedding identity")
        await verify_stores(generation, dense, sparse)
        return await self.activate(generation, expected_revision=expected_revision)


def selected_generation(flt: RetrievalFilter) -> IndexGeneration:
    generation = flt.index_generation
    if generation is None or generation.study_id != flt.study_id:
        raise ValueError("a study-scoped index generation must be selected before searching")
    return generation


async def verify_stores(generation: IndexGeneration, dense: GenerationSink,
                        sparse: GenerationSink) -> None:
    expected = IndexReceipt(generation.generation_id, generation.chunk_count,
                            generation.payload_sha256)
    for sink in (dense, sparse):
        if await sink.inspect(generation) != expected:
            raise Conflict("staged index content differs from the publication manifest")


async def publish_generation(registry: IndexRegistry, generation: IndexGeneration,
                             chunks: list[Chunk], vectors: list[list[float]], *,
                             dense: GenerationSink, sparse: GenerationSink,
                             evaluate: Callable[[IndexGeneration], Awaitable[bool]],
                             expected_revision: str | None, evidence) -> str:
    study_key = hashlib.sha256(generation.study_id.encode()).hexdigest()[:16]
    if (generation.dense_collection != f"csr_{study_key}_{generation.generation_id}" or
            generation.sparse_generation != generation.generation_id):
        raise ValueError("generation storage names must be immutable and study-specific")
    existing = await registry.state.get("validated_index", generation.study_id,
                                        generation.generation_id)
    if existing:
        raise Conflict("published/validated generations cannot be staged again")
    try:
        await registry.state.put("index_intent", generation.study_id, generation.generation_id,
                                 generation.model_dump(), expected_revision=None)
    except Conflict:
        intent = await registry.state.get("index_intent", generation.study_id,
                                         generation.generation_id)
        if not intent or intent.value != generation.model_dump():
            raise Conflict("generation identity already names a different payload") from None
    if len({c.id for c in chunks}) != len(chunks):
        raise ValueError("duplicate chunk identities")
    if len(chunks) != len(vectors) or any(len(v) != generation.dimensions for v in vectors):
        raise ValueError("embedding dimensions/count do not match the generation")
    if any(c.study_id != generation.study_id or c.parser_version != generation.parser_version
           or not c.source_revision for c in chunks):
        raise ValueError("chunks must match the generation study/parser and identify a source")
    if payload_digest(chunks) != generation.payload_sha256 or len(chunks) != generation.chunk_count:
        raise ValueError("chunks do not match the publication manifest")
    for chunk in chunks:
        await evidence.archive_chunk(chunk)
    await dense.stage(generation, chunks, vectors)
    await sparse.stage(generation, chunks, vectors)
    await verify_stores(generation, dense, sparse)
    if not await evaluate(generation):
        raise Conflict("index evaluation rejected publication")
    await evidence.retain_generation(generation, chunks)
    await registry.register_validated(generation)
    return await registry.activate(generation, expected_revision=expected_revision)

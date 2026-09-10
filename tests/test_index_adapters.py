import copy
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient

from medw_core.indexing import make_generation, payload_digest
from medw_core.local.indexes import DurableSparseIndex, SQLiteGenerationSink
from medw_core.persistence import Conflict, SQLiteStateStore
from medw_core.schemas import Chunk, DocType, RetrievalFilter
from pipelines.sinks.qdrant_sink import QdrantGenerationSink
from pipelines.sinks.search_sink import SearchGenerationSink


def chunk():
    return Chunk(id="34dbe790-15ee-5f83-a2c3-d8c8b7d07a2a", study_id="S1", doc_id="doc",
                 doc_type=DocType.tfl, section_path="11.4.2", kind="prose", ordinal=0,
                 text="synthetic original", source_revision="r1", source_location="page 1")


def manifest(chunks):
    return make_generation("S1", chunks, parser_version="p7", embed_version="local-hash-000",
                           embed_deployment="local-hash-000", embed_model_version="1", dimensions=4)


async def test_qdrant_stage_readback_detects_payload_mutation():
    client = AsyncQdrantClient(location=":memory:")
    sink = QdrantGenerationSink(client)
    c = chunk()
    generation = manifest([c])
    with pytest.warns(UserWarning, match="no effect"):
        receipt = await sink.stage(generation, [c], [[1.0, 0.0, 0.0, 0.0]])
    assert receipt.payload_sha256 == payload_digest([c])
    # This mutation leaves chunk IDs and text untouched: ID-only reconciliation misses it.
    await client.set_payload(generation.dense_collection, {"section_prefixes": ["wrong"]}, [c.id])
    with pytest.raises(Conflict, match="payload"):
        await sink.inspect(generation)
    await client.close()


class SearchDouble:
    def __init__(self):
        self.docs = []
        self.fail = False

    async def upload_documents(self, documents):
        self.docs = copy.deepcopy(documents)
        return [SimpleNamespace(succeeded=not self.fail) for _ in documents]

    async def search(self, *, search_text, filter):
        assert "study_id eq 'S1'" in filter and "index_generation eq" in filter

        async def pages():
            for doc in self.docs:
                yield doc
        return pages()


async def test_search_partial_failure_and_payload_readback():
    client = SearchDouble()
    sink = SearchGenerationSink(client)
    c = chunk()
    generation = manifest([c])
    client.fail = True
    with pytest.raises(Conflict, match="rejected"):
        await sink.stage(generation, [c], [[0.0] * 4])
    client.fail = False
    receipt = await sink.stage(generation, [c], [[0.0] * 4])
    assert receipt.count == 1
    client.docs[0]["text"] = "different text, same evidence_chunk_id"
    with pytest.raises(Conflict, match="payload"):
        await sink.inspect(generation)


async def test_durable_sparse_reader_only_sees_selected_generation_after_restart(tmp_path):
    path = tmp_path / "indexes.sqlite3"
    state = SQLiteStateStore(path)
    sink = SQLiteGenerationSink(state)
    old_chunk = chunk()
    old = manifest([old_chunk])
    new_chunk = old_chunk.model_copy(update={"text": "synthetic replacement"})
    new = manifest([new_chunk])
    await sink.stage(old, [old_chunk], [[0.0] * 4])
    await sink.stage(new, [new_chunk], [[0.0] * 4])
    await state.close()
    reopened = SQLiteStateStore(path)
    reader = DurableSparseIndex(reopened)
    flt = RetrievalFilter(study_id="S1", section_prefix="11.4", index_generation=old)
    assert (await reader.search("original", flt, limit=5))[0][2]["text"] == old_chunk.text
    assert await reader.search("replacement", flt, limit=5) == []
    with pytest.raises(ValueError, match="generation"):
        await reader.search("original", RetrievalFilter(study_id="S1"), limit=5)
    with pytest.raises(RuntimeError, match="cannot write"):
        await reader.index([new_chunk])
    await reopened.close()

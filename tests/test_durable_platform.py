"""Failure-path proofs for durable infrastructure; all clinical inputs are synthetic."""

import dataclasses
import json
import sqlite3

import pytest
from sqlalchemy import text

from medw_core.audit_events import AUDIT_COLUMNS, generation_event
from medw_core.durable_jobs import DurableJobStore, run_stages
from medw_core.ids import chunk_id
from medw_core.indexing import (
    IndexReceipt,
    IndexRegistry,
    make_generation,
    payload_digest,
    publish_generation,
)
from medw_core.local.durable_audit import SQLiteAuditSink
from medw_core.persistence import Conflict, SQLiteStateStore
from medw_core.provenance import Provenance
from medw_core.schemas import Chunk, DocType
from medw_core.sources import EvidenceStore, LocalArtifacts
from medw_core.sql import AUDIT_INSERT, SqlAuditSink


class SyntheticSink:
    def __init__(self):
        self.chunks = {}
        self.fail = False

    async def stage(self, generation, chunks, vectors):
        if self.fail:
            raise RuntimeError("injected partial indexing failure")
        self.chunks[generation.generation_id] = [c.model_copy(deep=True) for c in chunks]
        return await self.inspect(generation)

    async def inspect(self, generation):
        chunks = self.chunks.get(generation.generation_id, [])
        return IndexReceipt(generation.generation_id, len(chunks), payload_digest(chunks))


async def accepted(generation):
    return True


async def rejected(generation):
    return False


@pytest.fixture
async def platform(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    evidence = EvidenceStore(state, LocalArtifacts(tmp_path / "artifacts"))
    yield state, evidence, IndexRegistry(state)
    await state.close()


async def revision_chunk(evidence, payload=b"synthetic original", *, study="S1"):
    revision = await evidence.ingest_source(study, "doc1", payload, "synthetic.txt")
    return Chunk(id=chunk_id(study, "doc1", "11.4", 0, source_revision=revision.revision_id),
                 study_id=study, doc_id="doc1", doc_type=DocType.tfl, section_path="11.4",
                 kind="prose", text=payload.decode(), ordinal=0,
                 source_revision=revision.revision_id, parser_version="p7", source_location="page 1")


def manifest(chunks):
    return make_generation("S1", chunks, parser_version="p7", embed_version="local-hash-000",
                           embed_deployment="local-hash-000", embed_model_version="1", dimensions=4)


async def publish(registry, evidence, generation, chunks, dense, sparse, revision=None,
                  evaluate=accepted):
    return await publish_generation(registry, generation, chunks, [[0.0] * 4 for _ in chunks],
                                     dense=dense, sparse=sparse, evaluate=evaluate,
                                     expected_revision=revision, evidence=evidence)


async def test_source_edit_preserves_old_citation_after_restart(platform, tmp_path):
    _, evidence, registry = platform
    old = await revision_chunk(evidence)
    duplicate = await revision_chunk(evidence)
    assert duplicate.id == old.id
    citation = await evidence.archive_chunk(old)
    dense, sparse = SyntheticSink(), SyntheticSink()
    first_revision = await publish(registry, evidence, manifest([old]), [old], dense, sparse)
    new = await revision_chunk(evidence, b"synthetic edited")
    assert new.source_revision != old.source_revision and new.id != old.id
    await evidence.archive_chunk(new)
    await publish(registry, evidence, manifest([new]), [new], dense, sparse, first_revision)
    await evidence.retain_for_event("event-1", [citation])
    reopened = SQLiteStateStore(tmp_path / "state.sqlite3")
    restored = EvidenceStore(reopened, LocalArtifacts(tmp_path / "artifacts"))
    assert (await restored.resolve(citation))[2] == b"synthetic original"
    with pytest.raises(Conflict, match="immutable"):
        await evidence.archive_chunk(old.model_copy(update={"text": "tampered"}))
    await reopened.close()


async def test_publication_failures_preserve_active_generation(platform):
    _, evidence, registry = platform
    chunks = [await revision_chunk(evidence)]
    dense, sparse = SyntheticSink(), SyntheticSink()
    original = manifest(chunks)
    revision = await publish(registry, evidence, original, chunks, dense, sparse)
    replacement = manifest(chunks)
    sparse.fail = True
    with pytest.raises(RuntimeError, match="partial"):
        await publish(registry, evidence, replacement, chunks, dense, sparse, revision)
    assert (await registry.active("S1"))[0] == original
    sparse.fail = False
    with pytest.raises(Conflict, match="evaluation"):
        await publish(registry, evidence, replacement, chunks, dense, sparse, revision,
                      evaluate=rejected)
    assert (await registry.active("S1"))[0] == original
    await publish(registry, evidence, replacement, chunks, dense, sparse, revision)
    with pytest.raises(Conflict, match="embedding"):
        await registry.select("S1", embed_version="wrong", embed_deployment="local-hash-000",
                               dimensions=4, embed_model_version="1")


async def test_selection_is_stable_and_rollback_is_guarded(platform):
    _, evidence, registry = platform
    chunks = [await revision_chunk(evidence)]
    dense, sparse = SyntheticSink(), SyntheticSink()
    old, new = manifest(chunks), manifest(chunks)
    revision = await publish(registry, evidence, old, chunks, dense, sparse)
    selected = await registry.select("S1", embed_version="local-hash-000",
                                    embed_deployment="local-hash-000", dimensions=4,
                                    embed_model_version="1")
    current = await publish(registry, evidence, new, chunks, dense, sparse, revision)
    assert selected == old
    kwargs = {"dense": dense, "sparse": sparse, "embed_version": "local-hash-000",
              "embed_deployment": "local-hash-000", "dimensions": 4, "embed_model_version": "1"}
    with pytest.raises(Conflict):
        await registry.rollback("S1", old.generation_id, expected_revision=revision, **kwargs)
    dense.chunks[old.generation_id][0].text = "same ID, different payload"
    with pytest.raises(Conflict, match="content"):
        await registry.rollback("S1", old.generation_id, expected_revision=current, **kwargs)
    dense.chunks[old.generation_id] = chunks
    await registry.rollback("S1", old.generation_id, expected_revision=current, **kwargs)
    assert (await registry.active("S1"))[0] == old


async def test_job_lease_cas_and_restart_preserve_checkpoint(tmp_path):
    now = [100.0]
    state = SQLiteStateStore(tmp_path / "jobs.sqlite3")
    jobs = DurableJobStore(state, clock=lambda: now[0])
    job = await jobs.create("S1", "d1", source_revision="r1", idempotency_key="upload1")
    assert (await jobs.create("S1", "d1", source_revision="r1", idempotency_key="upload1"))["id"] == job["id"]
    with pytest.raises(Conflict):
        await jobs.create("S1", "d2", idempotency_key="upload1")
    job = await jobs.claim("S1", job["id"], "worker1", lease_seconds=10)
    with pytest.raises(Conflict):
        await jobs.claim("S1", job["id"], "worker2")
    stale = job
    job = await jobs.advance(job, "extracting")
    with pytest.raises(Conflict):
        await jobs.advance(stale, "extracting")
    job = await jobs.checkpoint(job, "extracting", "sha256:retained-layout")
    await state.close()
    now[0] = 111
    reopened = SQLiteStateStore(tmp_path / "jobs.sqlite3")
    resumed = DurableJobStore(reopened, clock=lambda: now[0])
    assert len(await resumed.recoverable("S1")) == 1
    replacement = await resumed.claim("S1", job["id"], "worker2")
    assert replacement["checkpoints"] == {"extracting": "sha256:retained-layout"}
    with pytest.raises(Conflict):
        await resumed.checkpoint(job, "bad", "uri")
    await reopened.close()


async def test_synthetic_stage_resume_skips_committed_work(tmp_path):
    state = SQLiteStateStore(tmp_path / "jobs.sqlite3")
    now = [0.0]
    jobs = DurableJobStore(state, clock=lambda: now[0])
    job = await jobs.create("S1", "d1")
    job = await jobs.claim("S1", job["id"], "crashed", lease_seconds=1)
    job = await jobs.advance(job, "extracting")
    job = await jobs.checkpoint(job, "extracting", "cached")
    now[0] = 2
    calls = []

    async def callback(current):
        calls.append(current["state"])
        return f"artifact:{current['state']}"

    stages = dict.fromkeys(["extracting", "classifying", "chunking", "annotating",
                            "embedding", "indexing"], callback)
    done = await run_stages(jobs, job, "replacement", stages)
    assert done["state"] == "done" and "extracting" not in calls
    assert len(done["checkpoints"]) == 6
    await state.close()


async def test_audit_roundtrip_retains_all_axes_and_prevents_mutation(platform, tmp_path):
    _, evidence, _ = platform
    chunk = await revision_chunk(evidence)
    citation = await evidence.archive_chunk(chunk)
    generation = manifest([chunk])
    await evidence.retain_generation(generation, [chunk])
    prov = Provenance("test", "generation", "a" * 40, "chat-v1", "wrong-index",
                      "b" * 40, "7", image_digest="sha256:" + "c" * 64,
                      release_bundle_sha="d" * 64)
    event = generation_event(prov, generation, citations=[citation], correlation_id="e" * 32,
                              section_path="11.4", user_oid="synthetic-writer",
                              output_text="Synthetic 12 (8.5%)", numeric_ok=True, structural_ok=True)
    sink = SQLiteAuditSink(tmp_path / "audit.sqlite3", evidence)
    event_id = await sink.record_generation(event)
    await sink.close()
    connection = sqlite3.connect(tmp_path / "audit.sqlite3")
    row = json.loads(connection.execute("SELECT event_json FROM platform_audit WHERE event_id=?",
                                        (event_id,)).fetchone()[0])
    assert row.keys() >= set(AUDIT_COLUMNS)
    assert row["image_digest"] == prov.image_digest
    assert row["embed_version"] == generation.embed_version != prov.embed_version
    assert json.loads(row["index_manifest"])["generation_id"] == generation.generation_id
    for statement in ("DELETE FROM platform_audit", "UPDATE platform_audit SET event_json='{}'"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
    connection.close()
    assert set(text(AUDIT_INSERT)._bindparams) >= {f.name for f in dataclasses.fields(prov)}


async def test_sql_adapter_passes_complete_validated_row(platform):
    _, evidence, _ = platform
    chunk = await revision_chunk(evidence)
    citation = await evidence.archive_chunk(chunk)
    generation = manifest([chunk])
    await evidence.retain_generation(generation, [chunk])
    event = generation_event(Provenance("test", "generation", "a" * 40, "chat", "embed", "b" * 40, "1"),
                              generation, citations=[citation], correlation_id="c" * 32,
                              section_path="11.4", user_oid="user", output_text="12",
                              numeric_ok=True, structural_ok=False)

    class Transaction:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def execute(self, statement, row):
            assert set(statement._bindparams) == set(row) == set(AUDIT_COLUMNS)
            assert row["structural_check_passed"] is False

    class Engine:
        def begin(self):
            return Transaction()

    await SqlAuditSink(Engine(), evidence).record_generation(event)

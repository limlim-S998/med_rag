"""Exercise offline persistence and the shared durable ingestion workflow."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from support.audit import SQLiteAuditSink
from support.files import FileArtifacts, FileUploadStorage
from support.indexes import SQLiteGenerationSink
from support.state import SQLiteStateStore
from support.stores import PersistentDocumentStore

from medw_core.durable_jobs import DurableJobStore
from medw_core.indexing import IndexRegistry
from medw_core.ingestion import IngestionRunner
from medw_core.placeholders import (
    HashEmbedder,
    PlaceholderLayoutExtractor,
    PlaceholderTableClassifier,
)
from medw_core.schemas import TableType
from medw_core.settings import Settings
from medw_core.sources import EvidenceStore
from medw_core.uploads import UploadRequest, UploadService


def workflow(tmp_path, *, state=None):
    state = state or SQLiteStateStore(tmp_path / "state.db")
    evidence = EvidenceStore(state, FileArtifacts(tmp_path / "artifacts"))
    uploads = UploadService(state, FileUploadStorage(tmp_path / "staging"))
    audit = SQLiteAuditSink(tmp_path / "state.db")
    values = {"state": state, "evidence": evidence, "uploads": uploads,
              "jobs": DurableJobStore(state), "index_registry": IndexRegistry(state),
              "documents": PersistentDocumentStore(state), "audit": audit,
              "embedder": HashEmbedder(64), "layout": PlaceholderLayoutExtractor(evidence.artifacts),
              "classifier": PlaceholderTableClassifier()}
    services = SimpleNamespace(require=values.__getitem__)
    settings = Settings(_env_file=None, env="test", ingestion_poll_seconds=0.01)
    runner = IngestionRunner(settings, services, dense=SQLiteGenerationSink(state, "dense"),
                             sparse=SQLiteGenerationSink(state))
    return values, runner


async def upload(values, *, study="S1", doc="document", payload=b"Study evidence 12 patients.", key="one"):
    digest = hashlib.sha256(payload).hexdigest()
    uploads = values["uploads"]
    registered = await uploads.register(study, UploadRequest(
        filename="source.txt", doc_id=doc, size_bytes=len(payload), sha256=digest))
    record = await uploads.record(study, registered["upload_id"])
    await uploads.storage.write(record, payload)
    source = await uploads.capture(study, doc, registered["upload_id"], values["evidence"])
    job = await values["jobs"].create(study, doc, source_revision=source.revision_id,
                                        idempotency_key=key, correlation_id="1" * 32)
    return registered, source, job


async def test_extraction_and_classifier_are_called_and_recovery_preserves_actual_provenance(tmp_path):
    values, runner = workflow(tmp_path)
    _, source, job = await upload(values, payload=b"Registered source\x00bytes\xff 12 patients.")
    calls = []
    layout = values["layout"]

    class RecordingLayout:
        async def extract(self, source_uri, *, pages=None):
            calls.append(("layout", source_uri))
            return await layout.extract(source_uri, pages=pages)

    class RecordingClassifier:
        model_version = "recording-classifier-3"

        def classify(self, table):
            calls.append(("classifier", table))
            return TableType.other, 0.25

    values.update(layout=RecordingLayout(), classifier=RecordingClassifier())
    checkpoint = values["jobs"].checkpoint

    async def crash_after_classification(current, stage, uri):
        result = await checkpoint(current, stage, uri)
        if stage == "classifying":
            raise asyncio.CancelledError()
        return result

    values["jobs"].checkpoint = crash_after_classification
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once()
    assert calls[0] == ("layout", source.artifact_uri)
    assert calls[1][0] == "classifier" and len(calls) == 2
    envelope = calls[1][1]
    assert envelope.cells == [] and envelope.table_number == "placeholder-envelope"
    assert source.content_sha256 in envelope.title
    assert "Registered source bytes" in envelope.header_stack[0]
    interrupted = await values["jobs"].get("S1", job["id"])
    artifacts = values["evidence"].artifacts
    classification = json.loads(await artifacts.get(interrupted["checkpoints"]["classifying"]))
    assert classification["classifier_version"] == "recording-classifier-3"
    assert classification["table_type"] == "other" and classification["confidence"] == 0.25
    assert classification["input_kind"] == "placeholder_envelope"
    assert classification["medical_parsing"] is False
    assert classification["source_sha256"] == source.content_sha256

    # A replacement worker must use the saved model result, even if the current
    # dependency would produce different provenance or is temporarily absent.
    values["layout"] = values["classifier"] = None
    values["jobs"].checkpoint = checkpoint
    events = []
    record_index = values["audit"].record_index

    async def capture_index(event):
        events.append(event)
        await record_index(event)

    values["audit"].record_index = capture_index
    await runner.run_once()
    assert (await values["jobs"].get("S1", job["id"]))["state"] == "done"
    assert len(calls) == 2
    assert events[0]["document"]["classifier_version"] == "recording-classifier-3"
    document = (await values["documents"].by_study("S1"))[0]
    assert document["classification"] == classification
    await values["audit"].close()
    await values["state"].close()


async def test_placeholder_layout_preserves_bytes_and_bounds_decoded_content(tmp_path):
    artifacts = FileArtifacts(tmp_path)
    payload = b"\x00\xff\n" + b"a" * 64000
    uri = await artifacts.put(payload)
    extractor = PlaceholderLayoutExtractor(artifacts)
    result = await extractor.extract(uri)
    assert result["content"].startswith(" \ufffd\n") and len(result["content"]) == 64000
    assert result["tables"] == result["paragraphs"] == []
    assert result["truncated"] is True and result["medical_parsing"] is False
    assert result["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert await artifacts.get(uri) == payload
    with pytest.raises(ValueError, match="page selection"):
        await extractor.extract(uri, pages="1")


async def test_upload_registration_is_scoped_expiring_and_checks_content(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    clock = [1000.0]
    storage = FileUploadStorage(tmp_path / "staging")
    service = UploadService(state, storage, clock=lambda: clock[0])
    evidence = EvidenceStore(state, FileArtifacts(tmp_path / "artifacts"))
    body = UploadRequest(filename="../../source.bin", doc_id="doc", size_bytes=3,
                         sha256=hashlib.sha256(b"abc").hexdigest())
    registered = await service.register("S1", body)
    record = await service.record("S1", registered["upload_id"])
    with pytest.raises(KeyError):
        await service.record("S2", registered["upload_id"])
    with pytest.raises(KeyError):
        await service.record("S1", registered["upload_id"], "another-document")
    # Blob accepts arbitrary bytes; application validation happens on capture.
    await storage.write(record, b"bad")
    with pytest.raises(ValueError, match="checksum"):
        await service.capture("S1", "doc", registered["upload_id"], evidence)
    assert not (tmp_path / "source.bin").exists()
    clock[0] += 901
    with pytest.raises(PermissionError, match="expired"):
        await service.record("S1", registered["upload_id"])
    await state.close()


async def test_ingestion_publishes_both_stores_preserves_study_and_source_revisions(tmp_path):
    values, runner = workflow(tmp_path)
    _, first, job = await upload(values)
    assert await runner.run_once() == 1
    done = await values["jobs"].get("S1", job["id"])
    assert done["state"] == "done" and len(done["checkpoints"]) == 6
    first_gen, _ = await values["index_registry"].active("S1")
    assert await runner.dense.inspect(first_gen) == await runner.sparse.inspect(first_gen)
    _, second, _ = await upload(values, doc="other", payload=b"Second document 9 people.", key="two")
    await runner.run_once()
    second_gen, _ = await values["index_registry"].active("S1")
    assert second_gen.chunk_count == 2
    await upload(values, payload=b"Updated first document 20 patients.", key="three")
    await runner.run_once()
    third_gen, _ = await values["index_registry"].active("S1")
    assert third_gen.chunk_count == 2
    snapshot = await values["state"].get("local_dense_generation", "S1", third_gen.generation_id)
    texts = [c["text"] for c in snapshot.value["chunks"]]
    assert any("Updated first" in text for text in texts)
    assert any("Second document" in text for text in texts)
    assert not any("Study evidence" in text for text in texts)
    assert await values["evidence"].artifacts.get(first.artifact_uri) == b"Study evidence 12 patients."
    assert await values["state"].get("source", "S1", second.revision_id)
    assert len(await values["documents"].by_study("S1")) == 2
    assert len(await values["index_registry"].generations("S1")) == 3
    await values["audit"].close()
    await values["state"].close()


async def test_crash_after_publication_reuses_generation_and_idempotent_audit(tmp_path):
    values, runner = workflow(tmp_path)
    _, _, job = await upload(values)
    documents = values["documents"]

    class InterruptedMetadata:
        async def upsert(self, document):
            raise ConnectionError("simulated crash after SQL audit")

    values["documents"] = InterruptedMetadata()
    await runner.run_once()
    active, _ = await values["index_registry"].active("S1")
    attempted = await values["jobs"].get("S1", job["id"])
    assert active is not None and attempted["state"] == "indexing"
    assert len(attempted["checkpoints"]) == 5
    await values["jobs"]._save(attempted, next_attempt_at=0)
    await values["audit"].close()
    await values["state"].close()
    reopened, restarted = workflow(tmp_path)
    assert await restarted.run_once() == 1
    assert (await reopened["jobs"].get("S1", job["id"]))["state"] == "done"
    actual, _ = await reopened["index_registry"].active("S1")
    assert actual.generation_id == active.generation_id
    assert len(await reopened["index_registry"].generations("S1")) == 1
    count = reopened["audit"].connection.execute(
        "SELECT count(*) FROM platform_audit WHERE kind='index'").fetchone()[0]
    assert count == 1
    assert (await reopened["documents"].by_study("S1"))[0]["doc_id"] == "document"
    del documents
    await reopened["audit"].close()
    await reopened["state"].close()


async def test_recovering_job_blocks_later_study_publication_but_not_other_studies(tmp_path):
    values, runner = workflow(tmp_path)
    _, _, first = await upload(values)
    delayed = await values["jobs"].get("S1", first["id"])
    await values["jobs"]._save(delayed, next_attempt_at=10 ** 12)
    _, _, later = await upload(values, doc="later", key="two")
    _, _, independent = await upload(values, study="S2", key="three")
    assert await runner.run_once() == 1
    assert (await values["jobs"].get("S2", independent["id"]))["state"] == "done"
    assert (await values["jobs"].get("S1", later["id"]))["state"] == "queued"
    assert await runner.run_once() == 0
    await values["audit"].close()
    await values["state"].close()


async def test_cancelled_worker_recovers_same_publication_after_lease_release(tmp_path):
    values, runner = workflow(tmp_path)
    _, _, job = await upload(values)
    started = asyncio.Event()
    original = runner.sparse

    class HangingSink:
        async def stage(self, *args):
            started.set()
            await asyncio.Event().wait()

        async def inspect(self, generation):
            return await original.inspect(generation)

    runner.sparse = HangingSink()
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), 5)
    plan = await values["state"].get("job_publication", "S1", job["id"])
    assert (await values["index_registry"].active("S1"))[0] is None
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    runner.sparse = original
    await runner.run_once()
    active, _ = await values["index_registry"].active("S1")
    assert active.generation_id == plan.value["generation"]["generation_id"]
    assert (await values["jobs"].get("S1", job["id"]))["state"] == "done"
    await values["audit"].close()
    await values["state"].close()


async def test_ingest_http_rejects_unregistered_input_and_is_idempotent_after_expiry(tmp_path, monkeypatch):
    from medw_core.auth import Principal, study_user
    from services.ingestion_worker.app.main import app as worker_app

    # This test isolates upload semantics; signed identity and study denials are
    # exercised independently by the batch HTTP integration tests.
    monkeypatch.setitem(worker_app.dependency_overrides, study_user, lambda: Principal("writer"))
    values, _ = workflow(tmp_path)
    registered, source, job = await upload(values)
    previous = getattr(worker_app.state, "services", None)
    worker_app.state.services = SimpleNamespace(require=values.__getitem__)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker_app), base_url="http://test") as client:
            url = "/studies/S1/documents/document/ingest"
            body = {"upload_id": registered["upload_id"], "idempotency_key": "one"}
            values["uploads"].clock = lambda: 10 ** 12
            response = await client.post(url, json=body)
            assert response.status_code == 202 and response.json()["id"] == job["id"]
            assert response.json()["source_revision"] == source.revision_id
            assert (await client.post(url.replace("S1", "S2"), json=body)).status_code == 404
            assert (await client.post(url, json={**body, "blob_path": "https://arbitrary"})).status_code == 422
            assert (await client.post(url, json={**body, "idempotency_key": "new"})).status_code == 403
    finally:
        worker_app.state.services = previous
        await values["audit"].close()
        await values["state"].close()


async def test_retired_local_http_endpoints_are_absent():
    from services.gateway.app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.put("/studies/S1/uploads/abc?token=unused", content=b"anything")
        assert response.status_code == 404
        assert (await client.get("/_synthetic/work")).status_code == 404


@pytest.mark.parametrize("stage", ["extracting", "classifying", "chunking", "annotating", "embedding", "indexing"])
async def test_restart_after_each_committed_checkpoint_does_not_repeat_stage(tmp_path, stage):
    values, runner = workflow(tmp_path)
    _, _, job = await upload(values)
    checkpoint = values["jobs"].checkpoint
    recorded = []

    async def crash_after_commit(current, name, uri):
        result = await checkpoint(current, name, uri)
        recorded.append(name)
        if name == stage:
            raise asyncio.CancelledError()
        return result

    values["jobs"].checkpoint = crash_after_commit
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once()
    interrupted = await values["jobs"].get("S1", job["id"])
    before = dict(interrupted["checkpoints"])

    async def count_checkpoint(current, name, uri):
        recorded.append(name)
        return await checkpoint(current, name, uri)

    values["jobs"].checkpoint = count_checkpoint
    await runner.run_once()
    completed = await values["jobs"].get("S1", job["id"])
    assert completed["state"] == "done"
    assert all(completed["checkpoints"][name] == uri for name, uri in before.items())
    assert len(recorded) == len(set(recorded)) == 6
    await values["audit"].close()
    await values["state"].close()


@pytest.mark.parametrize("method", ["register_validated", "activate"])
async def test_recovery_at_index_validation_and_active_pointer_boundaries(tmp_path, method):
    values, runner = workflow(tmp_path)
    _, _, job = await upload(values)
    original = getattr(values["index_registry"], method)

    async def crash_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("lost acknowledgement")

    setattr(values["index_registry"], method, crash_after_write)
    await runner.run_once()
    plan = await values["state"].get("job_publication", "S1", job["id"])
    attempted = await values["jobs"].get("S1", job["id"])
    await values["jobs"]._save(attempted, next_attempt_at=0)
    setattr(values["index_registry"], method, original)
    await runner.run_once()
    completed = await values["jobs"].get("S1", job["id"])
    active, _ = await values["index_registry"].active("S1")
    assert completed["state"] == "done"
    assert active.generation_id == plan.value["generation"]["generation_id"]
    assert len(await values["index_registry"].generations("S1")) == 1
    await values["audit"].close()
    await values["state"].close()


async def test_concurrent_workers_renew_lease_and_serialize_study_publication(tmp_path, monkeypatch):
    from medw_core import ingestion

    values, first = workflow(tmp_path)
    second = IngestionRunner(first.settings, first.services, dense=first.dense, sparse=first.sparse)
    # Local persistence performs real fsyncs. A 90ms wall-clock lease could
    # expire during hosted-runner disk I/O before asyncio could run a heartbeat.
    # Keep the production lease duration and control only time/timer wakeups.
    now = [1000.0]
    values["jobs"].clock = lambda: now[0]
    monkeypatch.setattr(ingestion, "time", SimpleNamespace(time=lambda: now[0]))
    heartbeat_waiting, tick, renewed_event = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def heartbeat_timer(seconds):
        assert seconds == first.lease_seconds / 3
        heartbeat_waiting.set()
        await tick.wait()
        tick.clear()

    monkeypatch.setattr(ingestion, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": heartbeat_timer}))
    renew = values["jobs"].renew

    async def observe_renewal(current, **kwargs):
        result = await renew(current, **kwargs)
        renewed_event.set()
        return result

    values["jobs"].renew = observe_renewal
    _, _, job = await upload(values)
    now[0] += 1
    _, _, later = await upload(values, doc="second", key="two")
    entered, released = asyncio.Event(), asyncio.Event()
    real_sink = first.dense

    class DelayedSink:
        async def stage(self, *args):
            entered.set()
            await released.wait()
            return await real_sink.stage(*args)

        async def inspect(self, generation):
            return await real_sink.inspect(generation)

    first.dense = DelayedSink()
    running = asyncio.create_task(first.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(heartbeat_waiting.wait(), 10)
        initial = await values["jobs"].get("S1", job["id"])
        now[0] += first.lease_seconds / 3
        tick.set()
        await asyncio.wait_for(renewed_event.wait(), 10)
        # The original claim has now expired, but both renewed leases must
        # still prevent another worker publishing this study concurrently.
        now[0] = initial["lease_until"] + 1
        assert await second.run_once() == 0
        renewed = await values["jobs"].get("S1", job["id"])
        study = await values["state"].get("ingestion_lease", "S1", "active")
        assert renewed["lease_owner"] == first.worker_id
        assert renewed["lease_until"] > now[0] > initial["lease_until"]
        assert study.value["owner"] == first.worker_id and study.value["until"] > now[0]
        released.set()
        await running
        assert await second.run_once() == 1
        assert (await values["jobs"].get("S1", later["id"]))["state"] == "done"
        generation, _ = await values["index_registry"].active("S1")
        assert generation.chunk_count == 2
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await values["audit"].close()
        await values["state"].close()


async def test_azure_upload_sas_is_one_blob_create_only_and_read_is_etag_bound(monkeypatch):
    from azure.core import MatchConditions

    from medw_core.uploads import AzureUploadStorage

    calls: dict = {}

    class Blob:
        url = "https://account.blob.core.windows.net/raw/staging/id"

        async def get_blob_properties(self):
            return SimpleNamespace(size=3, etag='"version-1"')

        async def download_blob(self, **kwargs):
            calls["download"] = kwargs

            async def readall():
                return b"abc"
            return SimpleNamespace(readall=readall)

    blob = Blob()

    class Service:
        account_name = "account"

        def get_container_client(self, name):
            calls["container"] = name
            return self

        def get_blob_client(self, name):
            calls["blob"] = name
            return blob

        async def get_user_delegation_key(self, start, expiry):
            calls["delegation"] = (start, expiry)
            return "delegated-key"

    def sas(**kwargs):
        calls["sas"] = kwargs
        return "signed"

    monkeypatch.setattr("azure.storage.blob.generate_blob_sas", sas)
    storage = AzureUploadStorage(Service(), "raw")
    record = {"blob_name": "staging/id", "expires_at": 2000000000, "size_bytes": 3}
    url = await storage.issue(record)
    assert url.endswith("?signed") and calls["sas"]["blob_name"] == "staging/id"
    assert str(calls["sas"]["permission"]) == "c"
    assert calls["sas"]["protocol"] == "https"
    assert await storage.read(record) == b"abc"
    assert calls["download"] == {"etag": '"version-1"', "match_condition": MatchConditions.IfNotModified}
    with pytest.raises(ValueError, match="size"):
        await storage.read({**record, "size_bytes": 4})

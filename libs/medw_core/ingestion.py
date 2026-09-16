"""Durable ingestion operations shared by the HTTP worker and future batch DAGs.

Medical parsing is deliberately simple. Storage, leases, publication and audit are
real: a restarted worker resumes the same source and immutable index generation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

from medw_core.context import CORRELATION_ID
from medw_core.ids import chunk_id
from medw_core.indexing import make_generation, publish_generation, verify_stores
from medw_core.jobs import JobState
from medw_core.persistence import Conflict
from medw_core.schemas import Chunk, DocType, IndexGeneration, ParsedTable, SourceRevision

log = logging.getLogger(__name__)


@dataclass
class Lease:
    job: dict
    study_revision: str


class IngestionRunner:
    def __init__(self, settings, services, *, dense, sparse, worker_id: str | None = None):
        self.settings, self.services = settings, services
        self.dense, self.sparse = dense, sparse
        self.worker_id = worker_id or uuid.uuid4().hex
        self.state = services.require("state")
        self.jobs = services.require("jobs")
        self.evidence = services.require("evidence")
        self.registry = services.require("index_registry")
        self.mutex = asyncio.Lock()
        self.lease_seconds = settings.ingestion_lease_seconds

    async def serve(self) -> None:
        while True:
            try:
                count = await self.run_once()
            except Exception:
                log.exception("ingestion poll failed")
                count = 0
            if not count:
                await asyncio.sleep(self.settings.ingestion_poll_seconds)

    async def run_once(self) -> int:
        """Bounded discovery/processing; one job per poll bounds work per process."""
        # The oldest unfinished job owns its study's queue position even while
        # leased/backing off. Later jobs must not overtake recovery and lose the
        # document snapshot or overwrite an already-published generation.
        pending = sorted((row.value for row in await self.state.list("job")
                          if row.value["state"] not in ("done", "failed")),
                         key=lambda job: (job.get("created_at", job["updated_at"]), job["id"]))
        studies = set()
        for candidate in pending:
            study = candidate["study_id"]
            if study in studies:
                continue
            studies.add(study)
            if max(candidate["lease_until"], candidate.get("next_attempt_at", 0)) > time.time():
                continue
            try:
                lease = await self.claim(candidate)
            except Conflict:
                continue
            await self.run(lease)
            return 1
        return 0

    async def claim(self, job: dict) -> Lease:
        study_id = job["study_id"]
        old = await self.state.get("ingestion_lease", study_id, "active")
        if old and old.value["until"] > time.time():
            raise Conflict("another worker owns the study publication lease")
        body = {"owner": self.worker_id, "until": time.time() + self.lease_seconds}
        locked = await self.state.put("ingestion_lease", study_id, "active", body,
                                      expected_revision=old.revision if old else None)
        try:
            claimed = await self.jobs.claim(study_id, job["id"], self.worker_id,
                                             lease_seconds=self.lease_seconds)
        except BaseException:
            await self.state.put("ingestion_lease", study_id, "active", {**body, "until": 0},
                                 expected_revision=locked.revision)
            raise
        return Lease(claimed, locked.revision)

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            async with self.mutex:
                row = await self.state.put(
                    "ingestion_lease", lease.job["study_id"], "active",
                    {"owner": self.worker_id, "until": time.time() + self.lease_seconds},
                    expected_revision=lease.study_revision)
                lease.study_revision = row.revision
                lease.job = await self.jobs.renew(lease.job, lease_seconds=self.lease_seconds)

    async def _release(self, lease: Lease) -> None:
        with contextlib.suppress(Conflict):
            row = await self.state.get("ingestion_lease", lease.job["study_id"], "active")
            if row and row.value["owner"] == self.worker_id:
                await self.state.put("ingestion_lease", lease.job["study_id"], "active",
                                     {"owner": self.worker_id, "until": 0},
                                     expected_revision=row.revision)

    async def _checkpoint(self, lease: Lease, stage: str, work) -> dict:
        uri = lease.job["checkpoints"].get(stage)
        if uri:
            return json.loads(await self.evidence.artifacts.get(uri))
        async with self.mutex:
            if lease.job["state"] != stage:
                lease.job = await self.jobs.advance(lease.job, stage)
        result = await work()
        artifact = await self.evidence.artifacts.put(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
        async with self.mutex:
            lease.job = await self.jobs.checkpoint(lease.job, stage, artifact)
        return result

    async def run(self, lease: Lease) -> None:
        token = CORRELATION_ID.set(lease.job["correlation_id"])
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        operation = asyncio.create_task(self._process_traced(lease))
        try:
            finished, _ = await asyncio.wait({heartbeat, operation},
                                             return_when=asyncio.FIRST_COMPLETED)
            # An expired/lost lease cancels processing before another durable write.
            if heartbeat in finished:
                await heartbeat
            await operation
        except asyncio.CancelledError:
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation
            await self._retry(lease, "worker shutdown", count_attempt=False)
            raise
        except Exception as exc:
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await operation
            log.exception("ingestion job attempt failed", extra={"job_id": lease.job["id"]})
            await self._retry(lease, type(exc).__name__)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            await self._release(lease)
            CORRELATION_ID.reset(token)

    async def _process_traced(self, lease: Lease) -> None:
        from opentelemetry import trace
        # A queued attempt gets its own span after the submission request ends.
        # The persisted correlation ID connects retries and the original request.
        with trace.get_tracer("medwriter.ingestion").start_as_current_span(
            "ingestion.process", attributes={
                "medw.correlation_id": lease.job["correlation_id"],
                "medw.job_id": lease.job["id"],
            },
        ):
            await self._process(lease)

    async def _retry(self, lease: Lease, error: str, *, count_attempt: bool = True) -> None:
        async with self.mutex:
            current = await self.jobs.get(lease.job["study_id"], lease.job["id"])
            if not current or current["lease_owner"] != self.worker_id:
                return
            if current["state"] in ("done", "failed"):
                return
            attempts = current.get("attempts", 0) + int(count_attempt)
            try:
                if attempts >= self.settings.ingestion_max_attempts:
                    lease.job = await self.jobs.fail(current, current["state"], error)
                else:
                    lease.job = await self.jobs._save(
                        current, attempts=attempts, last_error=error,
                        lease_owner=None, lease_until=0,
                        next_attempt_at=time.time() + (min(30, 2 ** attempts) if count_attempt else 0))
            except Conflict:
                # A replacement worker owns recovery after a lost lease.
                return

    async def _process(self, lease: Lease) -> None:
        job = lease.job
        source_row = await self.state.get("source", job["study_id"], job["source_revision"])
        if not source_row:
            raise LookupError("durably acknowledged source is missing")
        source = SourceRevision.model_validate(source_row.value)
        payload = await self.evidence.artifacts.get(source.artifact_uri)
        if hashlib.sha256(payload).hexdigest() != source.content_sha256:
            raise Conflict("source checksum mismatch")

        async def extracting():
            layout = await self.services.require("layout").extract(source.artifact_uri)
            if layout["source_sha256"] != source.content_sha256:
                raise Conflict("extracted source checksum mismatch")
            return {"text": layout["content"], "filename": source.filename,
                    "source_sha256": layout["source_sha256"],
                    "implementation": layout["implementation"],
                    "truncated": layout["truncated"], "medical_parsing": layout["medical_parsing"]}

        extracted = await self._checkpoint(lease, "extracting", extracting)

        async def classifying():
            # Exercise the existing table-classifier interface with an explicitly
            # synthetic envelope. Empty cells never imply a parsed clinical table.
            classifier = self.services.require("classifier")
            envelope = ParsedTable(
                table_number="placeholder-envelope",
                title=f"{source.filename}; sha256:{source.content_sha256}",
                header_stack=[extracted["text"][:1000]], cells=[])
            table_type, confidence = classifier.classify(envelope)
            return {"doc_type": "tfl", "doc_type_source": "placeholder_storage_category",
                    "classification": "placeholder", "input_kind": "placeholder_envelope",
                    "table_type": table_type.value, "confidence": confidence,
                    "classifier_version": classifier.model_version,
                    "source_sha256": source.content_sha256,
                    "medical_parsing": False, "medical_validated": False}

        classification = await self._checkpoint(lease, "classifying", classifying)

        async def chunking():
            header = f"Placeholder extraction: {source.filename}; sha256:{source.content_sha256}\n"
            text = extracted["text"] or "Binary source; no medical parsing performed."
            chunks = []
            for ordinal, offset in enumerate(range(0, len(text), 1000)):
                chunks.append(Chunk(
                    id=chunk_id(source.study_id, source.doc_id, "uploaded", ordinal,
                                source_revision=source.revision_id,
                                parser_version=self.settings.parser_version),
                    study_id=source.study_id, doc_id=source.doc_id,
                    doc_type=DocType(classification["doc_type"]), section_path="uploaded",
                    kind="prose", text=header + text[offset:offset + 1000], ordinal=ordinal,
                    source_revision=source.revision_id, parser_version=self.settings.parser_version,
                    source_location=f"placeholder decoded characters {offset}:{offset + 1000}").model_dump())
            return {"chunks": chunks}

        chunked = await self._checkpoint(lease, "chunking", chunking)

        async def annotating():
            return {**chunked, "annotation": "placeholder-no-medical-entities"}

        annotated = await self._checkpoint(lease, "annotating", annotating)

        async def embedding():
            vectors = await self.services.require("embedder").embed([c["text"] for c in annotated["chunks"]])
            return {"chunks": annotated["chunks"], "vectors": vectors}

        embedded = await self._checkpoint(lease, "embedding", embedding)

        async def indexing():
            return await self._publish(lease, source, embedded, classification)

        await self._checkpoint(lease, "indexing", indexing)
        async with self.mutex:
            lease.job = await self.jobs.advance(lease.job, JobState.done.value)

    async def _publish(self, lease: Lease, source: SourceRevision, embedded: dict,
                       classification: dict) -> dict:
        job = lease.job
        publication = await self.state.get("job_publication", source.study_id, job["id"])
        if publication:
            plan = publication.value
        else:
            active, revision = await self.registry.active(source.study_id)
            chunks = []
            if active is not None:
                memberships = await self.state.list("generation_evidence", source.study_id)
                for membership in memberships:
                    if membership.value["manifest"]["generation_id"] != active.generation_id:
                        continue
                    row = await self.state.get("chunk_evidence", source.study_id,
                                               membership.value["chunk_id"])
                    if row is None:
                        raise Conflict("published generation evidence is missing")
                    if row.value["doc_id"] != source.doc_id:
                        chunks.append(Chunk.model_validate(row.value))
            chunks += [Chunk.model_validate(value) for value in embedded["chunks"]]
            # Re-embed retained chunks using the currently selected implementation.
            vectors = await self.services.require("embedder").embed([c.text for c in chunks])
            generation = make_generation(
                source.study_id, chunks, parser_version=self.settings.parser_version,
                embed_version=self.settings.embed_version, embed_deployment=self.settings.embed_deployment,
                embed_model_name=self.settings.embed_model_name,
                embed_model_version=self.settings.embed_model_version, dimensions=self.settings.embed_dim)
            artifact = await self.evidence.artifacts.put(json.dumps({
                "chunks": [c.model_dump() for c in chunks], "vectors": vectors},
                sort_keys=True, separators=(",", ":")).encode())
            # The study snapshot may exceed Cosmos's item size limit; keep its
            # immutable content in Blob and only its reference in platform state.
            plan = {"generation": generation.model_dump(), "artifact_uri": artifact,
                    "expected_revision": revision}
            await self.state.put("job_publication", source.study_id, job["id"], plan,
                                 expected_revision=None)
        generation = IndexGeneration.model_validate(plan["generation"])
        snapshot = json.loads(await self.evidence.artifacts.get(plan["artifact_uri"]))
        chunks = [Chunk.model_validate(value) for value in snapshot["chunks"]]
        active, revision = await self.registry.active(source.study_id)
        if active is None or active.generation_id != generation.generation_id:
            if revision != plan["expected_revision"]:
                raise Conflict("publication base changed; refusing to replace a newer generation")
            validated = await self.state.get("validated_index", source.study_id, generation.generation_id)
            if validated:
                await verify_stores(generation, self.dense, self.sparse)
                await self.registry.activate(generation, expected_revision=revision)
            else:
                async def structurally_valid(manifest):
                    return manifest.chunk_count > 0

                await publish_generation(
                    self.registry, generation, chunks, snapshot["vectors"], dense=self.dense,
                    sparse=self.sparse, evaluate=structurally_valid,
                    expected_revision=revision, evidence=self.evidence)
        # Deterministic event identity makes crash-after-SQL-commit recovery safe.
        audit_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"medw:index:{source.study_id}:{job['id']}"))
        await self.services.require("audit").record_index({
            "event_id": audit_id, "study_id": source.study_id, "doc_id": source.doc_id,
            "parser_version": generation.parser_version, "embed_version": generation.embed_version,
            "collection": generation.dense_collection, "chunks_upserted": generation.chunk_count,
            "index_generation_id": generation.generation_id, "source_revision": source.revision_id,
            "correlation_id": job["correlation_id"],
            "document": {"doc_type": classification["doc_type"], "blob_path": source.artifact_uri,
                         # Earlier checkpoints did not invoke a classifier. Do
                         # not retrospectively attribute their result to one.
                         "classifier_version": classification.get("classifier_version", "not-run")}})
        await self.services.require("documents").upsert({
            "id": hashlib.sha256(source.doc_id.encode()).hexdigest(), "study_id": source.study_id,
            "doc_id": source.doc_id, "filename": source.filename,
            "source_revision": source.revision_id, "source_sha256": source.content_sha256,
            "artifact_uri": source.artifact_uri, "index_generation_id": generation.generation_id,
            "parser_version": generation.parser_version, "ingested_at": time.time(),
            "job_id": job["id"], "audit_event_id": audit_id,
            "classification": classification, "medical_validated": False})
        return {"index_generation_id": generation.generation_id, "audit_event_id": audit_id,
                "source_revision": source.revision_id}

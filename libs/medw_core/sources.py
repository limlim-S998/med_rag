"""Immutable source bytes and citation evidence, independent of serving indexes.

There is deliberately no deletion API here. Retiring an index does not retire
evidence. Administrative study teardown is a separate, explicitly authorized
operation; no regulatory retention period is invented by this application.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile
from typing import Protocol

from medw_core.ids import source_revision_id
from medw_core.persistence import Conflict, StateStore
from medw_core.schemas import Chunk, Citation, IndexGeneration, SourceRevision


class Artifacts(Protocol):
    async def put(self, payload: bytes) -> str: ...
    async def get(self, uri: str) -> bytes: ...


class LocalArtifacts:
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                temporary = pathlib.Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise Conflict("content-addressed artifact was corrupted") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return f"sha256:{digest}"

    async def get(self, uri: str) -> bytes:
        digest = uri.removeprefix("sha256:")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid content-addressed artifact URI")
        payload = (self.root / digest).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise Conflict("artifact checksum mismatch")
        return payload


class EvidenceStore:
    def __init__(self, state: StateStore, artifacts: Artifacts):
        self.state, self.artifacts = state, artifacts

    async def _immutable(self, kind: str, study_id: str, key: str, value: dict) -> None:
        try:
            await self.state.put(kind, study_id, key, value, expected_revision=None)
        except Conflict:
            existing = await self.state.get(kind, study_id, key)
            if not existing or existing.value != value:
                raise Conflict(f"immutable {kind} identity reused with different contents") from None

    async def ingest_source(self, study_id: str, doc_id: str, payload: bytes,
                            filename: str) -> SourceRevision:
        digest = hashlib.sha256(payload).hexdigest()
        uri = await self.artifacts.put(payload)
        source = SourceRevision(
            study_id=study_id, doc_id=doc_id,
            revision_id=source_revision_id(study_id, doc_id, digest),
            content_sha256=digest, artifact_uri=uri, filename=filename)
        # Filename is presentation metadata: identical bytes remain one revision.
        existing = await self.state.get("source", study_id, source.revision_id)
        if existing:
            return SourceRevision.model_validate(existing.value)
        await self._immutable("source", study_id, source.revision_id, source.model_dump())
        return source

    async def archive_chunk(self, chunk: Chunk) -> Citation:
        source = await self.state.get("source", chunk.study_id, chunk.source_revision)
        if not source or source.value["doc_id"] != chunk.doc_id:
            raise ValueError("chunk source revision has not been registered for this document")
        if not chunk.source_location:
            raise ValueError("source location required for a citation")
        await self._immutable("chunk_evidence", chunk.study_id, chunk.id, chunk.model_dump())
        return Citation(study_id=chunk.study_id, chunk_id=chunk.id,
                        source_revision=chunk.source_revision, parser_version=chunk.parser_version,
                        source_location=chunk.source_location)

    async def resolve(self, citation: Citation) -> tuple[Chunk, SourceRevision, bytes]:
        record = await self.state.get("chunk_evidence", citation.study_id, citation.chunk_id)
        if not record:
            raise KeyError(citation.chunk_id)
        chunk = Chunk.model_validate(record.value)
        if (chunk.source_revision != citation.source_revision or
                chunk.parser_version != citation.parser_version or
                chunk.source_location != citation.source_location):
            raise Conflict("citation does not match the retained evidence")
        source = await self.state.get("source", citation.study_id, citation.source_revision)
        if not source:
            raise KeyError(citation.source_revision)
        revision = SourceRevision.model_validate(source.value)
        return chunk, revision, await self.artifacts.get(revision.artifact_uri)

    async def retain_for_event(self, event_id: str, citations: list[Citation]) -> None:
        for citation in citations:
            await self.resolve(citation)
            await self._immutable("citation_reference", citation.study_id,
                                  f"{event_id}:{citation.chunk_id}", citation.model_dump())

    async def retain_generation(self, generation: IndexGeneration, chunks: list[Chunk]) -> None:
        """Retain membership separately from the serving stores and active pointer."""
        from medw_core.indexing import payload_digest
        if payload_digest(chunks) != generation.payload_sha256 or len(chunks) != generation.chunk_count:
            raise ValueError("generation evidence does not match its manifest")
        for chunk in chunks:
            await self.archive_chunk(chunk)
            await self._immutable("generation_evidence", generation.study_id,
                                  f"{generation.generation_id}:{chunk.id}",
                                  {"manifest": generation.model_dump(), "chunk_id": chunk.id})

    async def validate_selection(self, generation: IndexGeneration,
                                  citations: list[Citation]) -> None:
        for citation in citations:
            membership = await self.state.get("generation_evidence", generation.study_id,
                                               f"{generation.generation_id}:{citation.chunk_id}")
            if not membership or membership.value["manifest"] != generation.model_dump():
                raise Conflict("citation is not evidence from the selected index generation")

"""The audit wire/storage contract; portable between SQL Server and SQLite."""

from __future__ import annotations

import hashlib
import json
import uuid

from medw_core.provenance import Provenance
from medw_core.schemas import Citation, IndexGeneration

AUDIT_COLUMNS = (
    "event_id", "correlation_id", "study_id", "section_path", "user_oid",
    "env", "service", "image_sha", "image_digest", "release_bundle_sha", "deployment_revision",
    "chat_deployment", "chat_model_name", "chat_model_version", "prompt_bundle_sha",
    "embed_version", "embed_model_name", "embed_model_version", "classifier_version",
    "index_generation_id", "index_manifest", "source_chunk_ids", "source_citations",
    "numeric_check_passed", "structural_check_passed", "output_text", "output_sha256",
)


def generation_event(provenance: Provenance, generation: IndexGeneration, *,
                     citations: list[Citation], correlation_id: str, section_path: str,
                     user_oid: str, output_text: str, numeric_ok: bool,
                     structural_ok: bool, event_id: str | None = None) -> dict:
    return {
        **provenance.as_dict(), "event_id": event_id or str(uuid.uuid4()),
        "correlation_id": correlation_id, "study_id": generation.study_id,
        "section_path": section_path, "user_oid": user_oid,
        "embed_version": generation.embed_version,
        "embed_model_version": generation.embed_model_version,
        "embed_model_name": generation.embed_model_name,
        "index_generation_id": generation.generation_id,
        "index_manifest": generation.model_dump(),
        "source_chunk_ids": [c.chunk_id for c in citations],
        "source_citations": [c.model_dump() for c in citations],
        "numeric_check_passed": numeric_ok, "structural_check_passed": structural_ok,
        "output_text": output_text, "output_sha256": hashlib.sha256(output_text.encode()).hexdigest(),
    }


def normalize_generation(event: dict) -> tuple[dict, list[Citation]]:
    row = {column: event[column] for column in AUDIT_COLUMNS}
    uuid.UUID(row["event_id"])
    if len(row["correlation_id"]) != 32:
        raise ValueError("correlation_id must contain 32 characters")
    manifest = row["index_manifest"]
    generation = IndexGeneration.model_validate(json.loads(manifest)
                                                if isinstance(manifest, str) else manifest)
    raw_citations = row["source_citations"]
    citations = [Citation.model_validate(c) for c in
                 (json.loads(raw_citations) if isinstance(raw_citations, str) else raw_citations)]
    raw_ids = row["source_chunk_ids"]
    ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    if not citations or ids != [c.chunk_id for c in citations]:
        raise ValueError("generation must carry its nonempty cited evidence")
    if (generation.study_id != row["study_id"] or
            generation.generation_id != row["index_generation_id"] or
            generation.embed_version != row["embed_version"] or
            generation.embed_model_version != row["embed_model_version"] or
            generation.embed_model_name != row["embed_model_name"]):
        raise ValueError("audit index identity disagrees with the selected retrieval manifest")
    if any(c.study_id != generation.study_id or c.parser_version != generation.parser_version
           or not c.source_revision or not c.source_location for c in citations):
        raise ValueError("citation is outside the selected study/parser")
    if hashlib.sha256(row["output_text"].encode()).hexdigest() != row["output_sha256"]:
        raise ValueError("output digest does not match retained output")
    row["index_manifest"] = generation.model_dump_json()
    row["source_chunk_ids"] = json.dumps(ids)
    row["source_citations"] = json.dumps([c.model_dump() for c in citations])
    return row, citations

# Deterministic chunk IDs. This is the difference between a pipeline you can
# re-run and one you have to clean up after.
#
# The ID is a pure function of (study, document, section path, ordinal,
# parser version). Re-running the DAG upserts the same points instead of
# duplicating them. Bumping PARSER_VERSION changes every ID, which is what
# you want after a parser fix: the backfill replaces rather than shadows.

import hashlib
import uuid

PARSER_VERSION = "p7"


def chunk_id(study_id: str, doc_id: str, section_path: str, ordinal: int,
             *, source_revision: str = "", parser_version: str = PARSER_VERSION) -> str:
    # JSON prevents delimiter collisions in client-supplied identifiers.
    import json
    raw = json.dumps([study_id, doc_id, source_revision, section_path, ordinal,
                      parser_version], separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    # Qdrant point IDs must be uint64 or UUID. UUIDv5-shaped from the hash.
    return str(uuid.UUID(bytes=digest[:16], version=5))


def collection_name(study_id: str, embed_version: str) -> str:
    # Embedding version in the name, not in the payload. If it were a payload
    # field you could accidentally search across two vector spaces and get
    # plausible-looking garbage.
    return f"csr_{study_id}_{embed_version}".lower().replace("-", "_")


def source_revision_id(study_id: str, doc_id: str, content_sha256: str) -> str:
    import json
    value = json.dumps([study_id, doc_id, content_sha256], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()

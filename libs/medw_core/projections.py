# How a Chunk is written into each store. One file, because there is exactly
# one right answer and it was previously given twice.
#
# The same chunk exists in three places at once: a vector in Qdrant, its text
# in Cognitive Search, and its structured ParsedTable spine in the Qdrant
# payload. Shared projections and generation payload verification keep these
# copies consistent; matching IDs alone is insufficient (README.md#major-decisions).
#
# What was NOT keeping them in step: the Qdrant payload was assembled inline in
# pipelines/sinks/qdrant_sink.py and the Search document inline in
# pipelines/sinks/search_sink.py, with nothing forcing the shared fields to
# agree. The retrieval service then merges results from both into one dict and
# reads `["text"]` and `["section_path"]` off whichever half returned the hit -
# so if one sink renamed a field, half the results would KeyError and the other
# half would be fine. That is the same failure as the section_prefix bug: two
# implementations of one idea, drifting quietly.
#
# The rule now: nothing constructs a store document by hand. If a projection
# needs a new field it is added here, and the tests fail everywhere that has
# not caught up - including against the real Cognitive Search index definition.

from typing import Any

from medw_core.schemas import Chunk, DocType, IndexGeneration, ParsedTable

# --- the shared contract -------------------------------------------------
#
# These keys mean the same thing in both stores and MUST be spelled the same
# way in both, because the retrieval service reads them off merged results
# without knowing which half a hit came from. `test_projections.py` asserts
# this set is present and identical in each projection.
SHARED_FIELDS = frozenset(
    {"study_id", "doc_id", "doc_type", "section_path", "kind", "text", "parser_version",
     "source_revision", "source_location", "section_prefixes"}
)

# What the retrieval service actually reads off a hit payload. Named here so
# the service imports the constants instead of spelling the strings again -
# a third place to drift.
TEXT_FIELD = "text"
SECTION_FIELD = "section_path"


def _shared(chunk: Chunk) -> dict[str, Any]:
    return {
        "study_id": chunk.study_id,
        "doc_id": chunk.doc_id,
        "doc_type": chunk.doc_type.value,
        "section_path": chunk.section_path,
        "kind": chunk.kind,
        "text": chunk.text,
        "parser_version": chunk.parser_version,
        "source_revision": chunk.source_revision,
        "source_location": chunk.source_location,
        "section_prefixes": [chunk.section_path[:i] for i in range(1, len(chunk.section_path) + 1)],
    }


# --- Qdrant --------------------------------------------------------------


def to_qdrant_payload(chunk: Chunk, coded_terms: list[str] | None = None) -> dict[str, Any]:
    """The payload that rides alongside the vector.

    `table` carries the full structured spine. table_to_text reads THAT, never
    a re-parse of `text` - a number that has been through a string round-trip
    is a number that can be wrong, and a wrong efficacy number in a submission
    is a finding rather than a typo.

    `ordinal` is here purely so the round-trip below is lossless. It is not
    filtered on and not indexed; reconstructing a Chunk from the store is what
    makes the reconciliation job and the backfill path possible without going
    back to the source document.
    """
    return {
        **_shared(chunk),
        "ordinal": chunk.ordinal,
        "coded_terms": chunk.coded_terms if coded_terms is None else coded_terms,
        "table": chunk.table.model_dump() if chunk.table else None,
    }


def from_qdrant_payload(point_id: str, payload: dict[str, Any]) -> Chunk:
    """Inverse of to_qdrant_payload.

    The point ID is passed separately because Qdrant stores it as the point's
    identity rather than as a payload field - it is not duplicated inside the
    payload, and duplicating it would create one more thing that can disagree.
    """
    table = payload.get("table")
    return Chunk(
        id=point_id,
        study_id=payload["study_id"],
        doc_id=payload["doc_id"],
        doc_type=DocType(payload["doc_type"]),
        section_path=payload["section_path"],
        kind=payload["kind"],
        text=payload["text"],
        table=ParsedTable.model_validate(table) if table else None,
        ordinal=payload["ordinal"],
        parser_version=payload["parser_version"],
        source_revision=payload.get("source_revision", ""),
        source_location=payload.get("source_location", ""),
        coded_terms=payload.get("coded_terms", []),
    )


# --- Azure Cognitive Search ----------------------------------------------


def to_search_document(chunk: Chunk, coded_terms: list[str] | None = None) -> dict[str, Any]:
    """The document uploaded to the `csr-chunks` index.

    Every key here must exist as a field in infra/search/csr-chunks-index.json.
    Cognitive Search rejects unknown fields on upload, so a mismatch is a hard
    failure at index time rather than a silent drop - but it fails during a
    long batch ingest, which is a slow and expensive way to find out. The test
    checks it against the index definition instead.

    `chunk_id` is the index key. Note it IS a field here, unlike Qdrant where
    identity lives outside the payload: two stores, two identity models, and
    the projection is where that difference is absorbed rather than leaked.
    """
    table: ParsedTable | None = chunk.table
    return {
        **_shared(chunk),
        "chunk_id": chunk.id,
        "table_number": table.table_number if table else None,
        "header_stack": table.header_stack if table else [],
        "footnotes": table.footnotes if table else [],
        # Clinical terms from Azure Language, indexed as a keyword collection.
        # This is what the sparse half matches literally - a MedDRA preferred
        # term is exactly what the embedding normalises away.
        "coded_terms": chunk.coded_terms if coded_terms is None else coded_terms,
    }


def to_search_action(chunk: Chunk, coded_terms: list[str] | None = None) -> dict[str, Any]:
    """Upload form: `mergeOrUpload`, never `upload`.

    Same idempotency property as the Qdrant upsert, and for the same reason -
    a DAG re-run after a parser fix must converge rather than duplicate.
    """
    return {"@search.action": "mergeOrUpload", **to_search_document(chunk, coded_terms)}


def to_generation_search_document(chunk: Chunk, generation: IndexGeneration) -> dict[str, Any]:
    return {**to_search_document(chunk),
            # Search's key must distinguish serving generations of the same evidence.
            "chunk_id": f"{generation.sparse_generation}_{chunk.id}",
            "evidence_chunk_id": chunk.id,
            "index_generation": generation.sparse_generation,
            "chunk_json": chunk.model_dump_json()}

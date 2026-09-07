# The two stores cannot diverge without one of these failing.
#
# The same chunk is written to Qdrant and to Cognitive Search, and the
# retrieval service merges hits from both into one dict and reads fields off
# it without knowing which store a hit came from. Before Phase B the two
# documents were assembled independently in two sink modules, so a renamed
# field would have made half the results KeyError and left the other half
# working - a bug that looks like flaky retrieval rather than a typo.

import json
import pathlib

import pytest

from medw_core.ids import chunk_id
from medw_core.projections import (
    SECTION_FIELD,
    SHARED_FIELDS,
    TEXT_FIELD,
    from_qdrant_payload,
    to_qdrant_payload,
    to_search_action,
    to_search_document,
)
from medw_core.schemas import Chunk, DocType, ParsedTable, TableCell

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX_DEF = ROOT / "infra" / "search" / "csr-chunks-index.json"


@pytest.fixture
def table_chunk() -> Chunk:
    table = ParsedTable(
        table_number="Table 14.3.2.1",
        title="Treatment-Emergent Adverse Events by SOC and PT",
        header_stack=["", "Placebo / n=142 / n (%)", "Active / n=145 / n (%)"],
        footnotes=["[1] Safety population.", "[2] MedDRA v26.0."],
        population="Safety",
        cells=[
            TableCell(row_label="Infections", arm="Placebo / n=142 / n (%)",
                      value="12 (8.5%)", n=12, pct=8.5),
            TableCell(row_label="Infections", arm="Active / n=145 / n (%)",
                      value="19 (13.1%)", n=19, pct=13.1),
        ],
    )
    return Chunk(
        id=chunk_id("ABC-101", "doc-7", "14.3.2", 0),
        study_id="ABC-101",
        doc_id="doc-7",
        doc_type=DocType.tfl,
        section_path="14.3.2",
        kind="table_rows",
        text="Table 14.3.2.1\nInfections | Placebo | 12 (8.5%)",
        table=table,
        ordinal=0,
    )


@pytest.fixture
def prose_chunk() -> Chunk:
    return Chunk(
        id=chunk_id("ABC-101", "doc-1", "11.4.2", 3),
        study_id="ABC-101",
        doc_id="doc-1",
        doc_type=DocType.prior_csr,
        section_path="11.4.2",
        kind="prose",
        text="[11.4.2] Subjects who discontinued early were handled as follows.",
        table=None,
        ordinal=3,
    )


# --- round trip ----------------------------------------------------------


@pytest.mark.parametrize("fixture", ["table_chunk", "prose_chunk"])
def test_qdrant_payload_round_trips_losslessly(fixture, request):
    """Chunk → payload → Chunk must be an identity.

    This is what lets the reconciliation job and the backfill path rebuild
    from the store instead of re-reading the source document - which matters
    because re-reading means paying Document Intelligence per page again.
    """
    chunk: Chunk = request.getfixturevalue(fixture)
    payload = to_qdrant_payload(chunk)
    assert from_qdrant_payload(chunk.id, payload) == chunk


def test_round_trip_preserves_the_numeric_spine(table_chunk):
    """The ParsedTable survives intact, cell for cell.

    table_to_text fills its template from these values and never from a
    re-parse of the chunk text. If the round trip dropped or coerced a cell,
    the numeric spine would be built from something that had been through a
    string conversion - which is the exact failure the design exists to stop.
    """
    restored = from_qdrant_payload(table_chunk.id, to_qdrant_payload(table_chunk))
    assert restored.table is not None
    assert restored.table.cells == table_chunk.table.cells
    assert restored.table.table_number == "Table 14.3.2.1"
    assert restored.table.footnotes == table_chunk.table.footnotes


# --- the two projections agree ------------------------------------------


@pytest.mark.parametrize("fixture", ["table_chunk", "prose_chunk"])
def test_both_projections_carry_the_shared_fields(fixture, request):
    chunk: Chunk = request.getfixturevalue(fixture)
    qdrant = to_qdrant_payload(chunk)
    search = to_search_document(chunk)

    assert qdrant.keys() >= SHARED_FIELDS
    assert search.keys() >= SHARED_FIELDS


@pytest.mark.parametrize("fixture", ["table_chunk", "prose_chunk"])
def test_shared_fields_hold_identical_values(fixture, request):
    """Not just present in both - equal in both.

    The retrieval service reads these off whichever half returned the hit, so
    a hit from Qdrant and the same hit from Search must be indistinguishable
    in every field it touches.
    """
    chunk: Chunk = request.getfixturevalue(fixture)
    qdrant = to_qdrant_payload(chunk)
    search = to_search_document(chunk)

    for field in SHARED_FIELDS:
        assert qdrant[field] == search[field], f"{field} differs between the two stores"


def test_fields_the_retrieval_service_reads_exist_in_both(table_chunk):
    """TEXT_FIELD and SECTION_FIELD are read off merged results in
    services/retrieval/app/main.py without checking the source."""
    for projection in (to_qdrant_payload(table_chunk), to_search_document(table_chunk)):
        assert TEXT_FIELD in projection
        assert SECTION_FIELD in projection


# --- the projection matches the real index definition -------------------


def _index_field_names() -> set[str]:
    return {f["name"] for f in json.loads(INDEX_DEF.read_text())["fields"]}


def test_every_search_field_exists_in_the_index_definition(table_chunk, prose_chunk):
    """Cognitive Search rejects unknown fields on upload.

    Without this test the failure surfaces mid-way through a long batch ingest
    against the real service - a slow and expensive way to discover that
    someone added a field to the projection and not to the index.
    """
    defined = _index_field_names()
    for chunk in (table_chunk, prose_chunk):
        produced = set(to_search_document(chunk))
        assert produced <= defined, f"not in the index definition: {sorted(produced - defined)}"


def test_the_index_key_is_populated(table_chunk):
    """chunk_id is the index key; a document without it is rejected."""
    assert to_search_document(table_chunk)["chunk_id"] == table_chunk.id


def test_search_upload_is_merge_or_upload(table_chunk):
    """`upload` would replace rather than merge, losing any field a later pass
    added. `mergeOrUpload` is what makes re-running a DAG converge."""
    assert to_search_action(table_chunk)["@search.action"] == "mergeOrUpload"


# --- coded terms --------------------------------------------------------


def test_coded_terms_reach_both_stores(prose_chunk):
    """The sparse half matches them literally and the verification pass diffs
    against them, so they have to be in both projections, not just the one."""
    terms = ["neutropenia", "C0027947"]
    assert to_qdrant_payload(prose_chunk, terms)["coded_terms"] == terms
    assert to_search_document(prose_chunk, terms)["coded_terms"] == terms


def test_coded_terms_default_to_empty_not_none(prose_chunk):
    """Cognitive Search types this as Collection(Edm.String). None is not an
    empty collection and the upload fails on it."""
    assert to_search_document(prose_chunk)["coded_terms"] == []
    assert to_qdrant_payload(prose_chunk)["coded_terms"] == []

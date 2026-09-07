# Domain types. These are the contract between ingestion, retrieval and
# generation - and, because they are Pydantic, they double as the JSON schema
# you paste into a prompt when you want structured output back.

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class DocType(StrEnum):
    protocol = "protocol"
    tfl = "tfl"
    prior_csr = "prior_csr"
    sap = "sap"


class TableType(StrEnum):
    demographics = "demographics"
    disposition = "disposition"
    ae_summary = "ae_summary"
    efficacy_endpoint = "efficacy_endpoint"
    other = "other"


class TableCell(BaseModel):
    row_label: str
    arm: str
    value: str          # kept as text: "12 (8.5%)" is one value, not two
    n: int | None = None
    pct: float | None = None


class ParsedTable(BaseModel):
    # The structured spine. This is what table-to-text reads from - never a
    # re-parse of the chunk string.
    table_number: str                 # "Table 14.3.2.1"
    title: str
    header_stack: list[str]
    footnotes: list[str] = Field(default_factory=list)
    population: str | None = None
    cells: list[TableCell]


class Chunk(BaseModel):
    id: str
    study_id: str
    doc_id: str
    doc_type: DocType
    section_path: str                 # "11.4.2" or "Efficacy > Primary"
    kind: Literal["prose", "table_rows"]
    text: str                         # self-describing: header stack prepended
    table: ParsedTable | None = None  # populated for kind == table_rows
    ordinal: int


class RetrievalFilter(BaseModel):
    """What to narrow a search to, expressed once, for every index.

    This type exists because the two halves of hybrid retrieval had drifted:
    the dense repo accepted a section prefix and the sparse repo did not, so a
    section-filtered query fused filtered dense hits with unfiltered sparse
    hits and quietly returned rows from the wrong part of the document.

    The fix is not "add the parameter to the other one" - that just resets the
    clock. It is that a filter is a domain concept with one definition, and
    each index translates it into its own dialect (Qdrant conditions, OData,
    a predicate over a local BM25 index). Adding a field here breaks every
    translator that has not handled it, which is exactly the failure you want.

    Note it is NOT typed as a string. An OData string would bake Cognitive
    Search into the abstraction meant to hide it, and no local index could
    ever satisfy the contract.
    """

    study_id: str
    doc_types: list[DocType] | None = None
    section_prefix: str | None = None
    kind: Literal["prose", "table_rows"] | None = None

    def doc_type_values(self) -> list[str] | None:
        # Translators want the wire values, not the enum members.
        return [d.value for d in self.doc_types] if self.doc_types else None


class Hit(BaseModel):
    chunk_id: str
    score: float
    text: str
    section_path: str
    source: Literal["dense", "sparse", "fused", "reranked"]


class RetrievalRequest(BaseModel):
    study_id: str
    query: str
    doc_types: list[DocType] | None = None
    section_prefix: str | None = None
    kind: Literal["prose", "table_rows"] | None = None
    top_k: int = 8

    def to_filter(self) -> RetrievalFilter:
        # One place converts the wire request into the domain filter, so the
        # two index halves cannot be handed different things by accident.
        return RetrievalFilter(
            study_id=self.study_id,
            doc_types=self.doc_types,
            section_prefix=self.section_prefix,
            kind=self.kind,
        )


class RetrievalResponse(BaseModel):
    hits: list[Hit]
    trace_id: str

# The seams. One Protocol per external dependency.
#
# These are the architecture: everything below them is a detail, and every one
# of them has at least two implementations - a real one that talks to Azure or
# Qdrant, and a local one that does not. That is what makes iterating free and
# switching a config change rather than a rewrite.
#
# Two rules were applied throughout, and both were learned the hard way:
#
#   1. No vendor dialect in a signature. `RetrievalFilter`, not an OData
#      string. A Protocol that only Cognitive Search can satisfy is not an
#      abstraction, it is Cognitive Search with extra steps.
#   2. No leaking SDK types. Return tuples and domain models, never a
#      `SearchItemPaged` or a `ScoredPoint`. The moment an SDK type crosses a
#      port, every consumer depends on that SDK.
#
# Protocols rather than ABCs, deliberately: the implementations do not inherit
# from anything, so a class in another package satisfies a port by shape
# alone. `@runtime_checkable` is here so the conformance tests can assert it,
# not so application code can - `isinstance` on a Protocol only checks that
# the method names exist, never the signatures, which makes it a weak check
# and a bad habit outside a test.

from typing import Protocol, runtime_checkable

from medw_core.schemas import Chunk, ParsedTable, RetrievalFilter, TableType

# (chunk_id, score, payload). A rank list, not a rich object: fusion only
# needs the ranks, and payloads are merged by the caller.
SearchResult = tuple[str, float, dict]


@runtime_checkable
class Embedder(Protocol):
    """Text to vectors.

    The invariant that matters is not in the signature and cannot be: the
    embedder used at query time MUST be the one used at index time. Different
    deployments produce different vector spaces, and searching across two of
    them returns confident garbage rather than an error. `embed_version`
    exists so that invariant is checkable - it is baked into the Qdrant
    collection name, so a mismatch fails loudly at lookup instead of quietly
    at ranking.
    """

    @property
    def embed_version(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batched. Implementations own their own batching and rate limiting."""
        ...


@runtime_checkable
class VectorIndex(Protocol):
    """Dense retrieval. Qdrant today; the port exists so that is a choice."""

    async def ensure_collection(self, study_id: str) -> str: ...

    async def search(
        self, vector: list[float], flt: RetrievalFilter, *, limit: int
    ) -> list[SearchResult]: ...

    async def upsert(
        self, study_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> int:
        """Idempotent. Chunk IDs are deterministic, so this overwrites in
        place - re-running a DAG converges rather than duplicating."""
        ...


@runtime_checkable
class SparseIndex(Protocol):
    """Lexical retrieval.

    This is the port that justifies the whole exercise. Cognitive Search takes
    an OData filter string; a local BM25 index has no such concept. Typing
    this as `filter: str` would have made the local implementation impossible
    to write, and you only discover that by trying to write it.

    It exists because embeddings normalise away exactly what must match
    literally: "Table 14.3.2.1", a MedDRA preferred term, "Grade 3", a lab
    parameter code. Those are the queries a medical writer actually types.
    """

    async def search(
        self, query: str, flt: RetrievalFilter, *, limit: int
    ) -> list[SearchResult]: ...

    async def index(self, chunks: list[Chunk]) -> int: ...


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder over (query, chunk) pairs. Returns (chunk_id, score),
    already sorted, already truncated to top_k."""

    async def rerank(
        self, query: str, candidates: list[tuple[str, str]], *, top_k: int
    ) -> list[tuple[str, float]]: ...


@runtime_checkable
class LayoutExtractor(Protocol):
    """Document geometry: tables as cells with row/column indices and spans,
    paragraphs with roles.

    Returns a plain dict in Document Intelligence's layout shape rather than a
    parsed model, because the local implementation is recorded fixtures of
    exactly that shape. That is what lets `parsers/table.py` - the code that
    makes this clinical rather than generic RAG - be exercised for real with
    no per-page extraction cost.
    """

    async def extract(self, source_uri: str, *, pages: str | None = None) -> dict: ...


@runtime_checkable
class EntityExtractor(Protocol):
    """Clinical entities, for the keyword index and the verification diff.

    Not a MedDRA coder: Azure Language links to UMLS CUIs, and the CUI to
    MedDRA PT mapping is a licensed table the sponsor owns. Saying otherwise
    out loud is the kind of thing a clinical reviewer catches.
    """

    async def extract(self, texts: list[str]) -> list[list[dict]]: ...


@runtime_checkable
class ChatClient(Protocol):
    """Generation. Streaming only - a section is thousands of tokens, and the
    wait for a complete response is the difference between a tool people use
    and one they close.

    The local implementation is a scripted responder, which is what makes the
    verification tests possible: to prove `numeric_fidelity` fails a tampered
    numeral you need a model that emits one on demand, and no real model does
    that reliably.
    """

    async def stream(self, prompt: str, *, max_tokens: int = 2048): ...

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        """Structured output. Implementations own the parse-validate-retry
        loop; a validation failure retries once with the error appended."""
        ...


@runtime_checkable
class TableClassifier(Protocol):
    """Which template the numeric spine fills. Load-bearing, not decoration:
    a wrong template narrates an efficacy table as an AE summary.

    Returns the label AND its confidence, because the calibrated abstention
    path below a threshold is the whole reason the model is calibrated.
    """

    @property
    def model_version(self) -> str: ...

    def classify(self, table: ParsedTable) -> tuple[TableType, float]: ...


@runtime_checkable
class JobStore(Protocol):
    """Ingestion state. Cosmos in the cloud, in-memory or SQLite locally."""

    async def create(self, study_id: str, doc_id: str) -> dict: ...

    async def advance(self, job: dict, state: str) -> dict: ...

    async def fail(self, job: dict, state: str, error: str) -> dict: ...

    async def get(self, study_id: str, job_id: str) -> dict | None: ...


@runtime_checkable
class AuditSink(Protocol):
    """The provenance record. Append-only by grant in Azure SQL; append-only
    by having no other method here.

    There is deliberately no update, no delete and no query-by-anything-mutable
    on this port. An audit trail you have merely promised not to modify is a
    policy; one whose interface cannot express modification is a property.
    """

    async def record_generation(self, event: dict) -> str: ...

    async def record_index(self, event: dict) -> str: ...

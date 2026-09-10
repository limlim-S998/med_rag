# The BM25 half, in Azure Cognitive Search.
#
# You need it because embeddings normalise away precisely what must match
# literally: "Table 14.3.2.1", a MedDRA preferred term, "Grade 3", a lab
# parameter code. Those are the queries a medical writer actually types.
#
# Satisfies medw_core.ports.SparseIndex by shape. The port takes a
# RetrievalFilter and this class translates it to OData - which is the reason
# the port is not typed as a filter string. An OData string in the signature
# would have made a local BM25 implementation impossible to write, and the
# whole point of the seam is that a second implementation exists.
#
# This class previously accepted only doc_types, so a section-filtered query
# returned section-scoped dense hits fused with unscoped sparse hits. Nothing
# errored; the ranking was just wrong. Sharing the filter type is what makes
# that class of divergence a type error instead of a silent one.

from azure.search.documents.aio import SearchClient

from medw_core.indexing import selected_generation
from medw_core.ports import SearchResult
from medw_core.schemas import RetrievalFilter


def _quote(value: str) -> str:
    # OData string literals escape a single quote by doubling it. Study IDs
    # and section paths are client-supplied, so this is not optional.
    return "'" + value.replace("'", "''") + "'"


class SparseRepo:
    def __init__(self, client: SearchClient):
        self.client = client

    def _odata(self, flt: RetrievalFilter) -> str:
        # The Cognitive Search dialect of RetrievalFilter. Unlike Qdrant,
        # study_id IS a filter here: one index holds every study,
        # so forgetting this clause would leak another sponsor's documents.
        # That asymmetry is the cost of the shared-index choice, and it is why
        # it lives in one function rather than at each call site.
        clauses = [f"study_id eq {_quote(flt.study_id)}"]
        if doc_types := flt.doc_type_values():
            ors = " or ".join(f"doc_type eq {_quote(d)}" for d in doc_types)
            clauses.append(f"({ors})")
        if flt.section_prefix:
            # search.ismatch would tokenise; section_path uses the keyword
            # analyser and a prefix match is what the caller means.
            clauses.append(f"section_prefixes/any(p: p eq {_quote(flt.section_prefix)})")
        if flt.kind:
            clauses.append(f"kind eq {_quote(flt.kind)}")
        if flt.index_generation is not None:
            generation = selected_generation(flt)
            clauses.append(f"index_generation eq {_quote(generation.sparse_generation)}")
        return " and ".join(clauses)

    async def search(self, query: str, flt: RetrievalFilter, *,
                      limit: int) -> list[SearchResult]:
        selected_generation(flt)
        results = await self.client.search(
            search_text=query,
            filter=self._odata(flt),
            top=limit,
            query_type="simple",
            search_fields=["text", "table_number", "section_path", "coded_terms"],
        )
        out: list[SearchResult] = []
        async for r in results:
            out.append((r["evidence_chunk_id"], r["@search.score"], dict(r)))
        return out

    async def index(self, chunks) -> int:
        # The write path lives in pipelines/sinks/search_sink.py; the service
        # has no write credential on the index. Present to satisfy the port,
        # and it raises rather than silently doing nothing.
        raise NotImplementedError(
            "retrieval has no write access to the search index; use pipelines.sinks.search_sink"
        )

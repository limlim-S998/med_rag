# In-memory stands-in for the two search halves and the extraction services.
#
# The BM25 here is a real, if small, implementation rather than a stub that
# returns nothing: the SparseIndex port was the one most at risk of being
# accidentally Azure-shaped, and only a second implementation proves it is not.
# It scores differently from Cognitive Search - different analyzer, no scoring
# profile - so it is a shape check, never a source of numbers.

import json
import math
import pathlib
import re
from collections import Counter

from medw_core.ports import SearchResult
from medw_core.projections import to_qdrant_payload
from medw_core.schemas import Chunk, RetrievalFilter

TOKEN = re.compile(r"[a-z0-9.]+")


def _tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class InMemorySparseIndex:
    """Satisfies medw_core.ports.SparseIndex. BM25 over a dict."""

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: dict[str, dict] = {}

    async def index(self, chunks: list[Chunk]) -> int:
        for c in chunks:
            self.docs[c.id] = {
                "chunk": c,
                "tokens": _tokens(c.text),
                "payload": to_qdrant_payload(c),
            }
        return len(chunks)

    def _matches(self, flt: RetrievalFilter, c: Chunk) -> bool:
        # The same filter object the Cognitive Search adapter translates to
        # OData. Every field must be honoured here too, or the two halves
        # diverge exactly the way they did before RetrievalFilter existed.
        if c.study_id != flt.study_id:
            return False
        if (dts := flt.doc_type_values()) and c.doc_type.value not in dts:
            return False
        if flt.section_prefix and not c.section_path.startswith(flt.section_prefix):
            return False
        return not (flt.kind and c.kind != flt.kind)

    async def search(self, query: str, flt: RetrievalFilter, *,
                     limit: int) -> list[SearchResult]:
        pool = {i: d for i, d in self.docs.items() if self._matches(flt, d["chunk"])}
        if not pool:
            return []
        n = len(pool)
        avgdl = sum(len(d["tokens"]) for d in pool.values()) / n
        df = Counter(t for d in pool.values() for t in set(d["tokens"]))

        scored: list[SearchResult] = []
        for cid, d in pool.items():
            tf, dl, score = Counter(d["tokens"]), len(d["tokens"]), 0.0
            for term in _tokens(query):
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denom = tf[term] + self.k1 * (1 - self.b + self.b * dl / avgdl)
                score += idf * (tf[term] * (self.k1 + 1)) / denom
            if score > 0:
                scored.append((cid, score, d["payload"]))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


class FixtureLayoutExtractor:
    """Satisfies medw_core.ports.LayoutExtractor by replaying recorded output.

    This is the bridge between local and real: the fixtures are Document
    Intelligence's own response shape, so the parser under test cannot tell
    the difference. Record once against the paid service, replay for free
    thereafter — which is also how you keep the fake honest as the real API
    changes.
    """

    def __init__(self, fixture_dir: str | pathlib.Path):
        self.dir = pathlib.Path(fixture_dir)

    async def extract(self, source_uri: str, *, pages: str | None = None) -> dict:
        name = pathlib.Path(source_uri).name
        path = self.dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"no recorded layout for {name} in {self.dir} - record it against "
                "the real service rather than hand-writing one"
            )
        return json.loads(path.read_text())


class DictionaryEntityExtractor:
    """Satisfies medw_core.ports.EntityExtractor with a term list.

    Not a clinical NER model and does not pretend to be. Azure Language links
    to UMLS CUIs; this matches literal strings. It exists so the coded_terms
    field is populated end to end and the plumbing is exercised.
    """

    def __init__(self, terms: list[str] | None = None):
        self.terms = [t.lower() for t in (terms or [
            "neutropenia", "anaemia", "nausea", "infection",
            "adverse event", "ecog", "placebo",
        ])]

    async def extract(self, texts: list[str]) -> list[list[dict]]:
        return [
            [{"text": t, "category": "Diagnosis", "confidence": 1.0, "cui": None}
             for t in self.terms if t in text.lower()]
            for text in texts
        ]


class InMemorySessionStore:
    """Satisfies medw_core.ports.SessionStore.

    Keyed by (user_id, session_id) rather than session_id alone, mirroring the
    /user_id partition. A local store keyed only by session_id would let a
    cross-user read pass here and fail against Cosmos.
    """

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    async def get(self, user_id: str, session_id: str) -> dict | None:
        return self.items.get((user_id, session_id))

    async def put(self, session: dict) -> None:
        self.items[(session["user_id"], session["session_id"])] = session


class InMemoryDocumentStore:
    """Satisfies medw_core.ports.DocumentStore."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    async def upsert(self, doc: dict) -> dict:
        # Upsert, not insert: document IDs are deterministic, so re-ingesting
        # overwrites rather than duplicating - the same property the chunk IDs
        # give the vector store.
        self.items[doc["doc_id"]] = doc
        return doc

    async def by_study(self, study_id: str) -> list[dict]:
        return [d for d in self.items.values() if d.get("study_id") == study_id]

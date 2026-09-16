"""Current model implementations, shared by local and Azure deployments.

These deterministic implementations exercise orchestration, never clinical
quality. Their identities use the existing model provenance fields.
"""

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator

from medw_core.schemas import ParsedTable, TableType
from medw_core.sources import Artifacts

MODEL_IDENTITIES = {
    "chat": {"deployment": "scripted-chat", "name": "scripted-placeholder", "version": "1"},
    "embedding": {"deployment": "hash-1", "name": "token-hash", "version": "1"},
    "classifier": {"name": "placeholder-table-classifier", "version": "placeholder-1"},
}

EMBED_VERSION = "hash-1"
DEFAULT_EMBED_DIMENSIONS = 64
PARSER_VERSION = "placeholder-text-1"


def validate_model_identities(chat: tuple[str, str, str],
                              embedding: tuple[str, str, str]) -> None:
    for kind, actual in (("chat", chat), ("embedding", embedding)):
        if actual != tuple(MODEL_IDENTITIES[kind][key] for key in ("deployment", "name", "version")):
            raise ValueError(f"configured {kind} identity differs from installed implementation")


class HashEmbedder:
    def __init__(self, dimensions: int = DEFAULT_EMBED_DIMENSIONS):
        if dimensions < 1:
            raise ValueError("embedding dimensions must be positive")
        self._dimensions = dimensions

    @property
    def embed_version(self) -> str:
        return EMBED_VERSION

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in text.lower().split():
                digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += (
                    1.0 if digest[4] & 1 else -1.0)
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class PlaceholderReranker:
    async def rerank(self, query: str, candidates: list[tuple[str, str]], *, top_k: int):
        terms = set(query.lower().split())
        ranked = [(identifier, float(len(terms.intersection(text.lower().split()))))
                  for identifier, text in candidates]
        return sorted(ranked, key=lambda row: (-row[1], row[0]))[:top_k]

    async def check(self) -> None:
        result = await self.rerank("a", [("one", "a")], top_k=1)
        if result != [("one", 1.0)]:
            raise RuntimeError("placeholder reranker self-check failed")


class PlaceholderChatClient:
    """Echo bounded evidence from the prompt; never invent clinical findings."""

    async def stream(self, prompt: str, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        identity = hashlib.sha256(prompt.encode()).hexdigest()
        evidence = prompt.rsplit("\nEVIDENCE:\n", 1)[-1]
        output = ("PLACEHOLDER DRAFT — medical verification not performed.\n"
                  f"Input/prompt SHA256: {identity}\nEvidence received:\n{evidence}")
        # Yield incrementally with cooperative scheduling; network buffering is
        # handled by ASGI/NGINX, not by sleeping to manufacture a slow model.
        for start in range(0, min(len(output), max_tokens * 4), 96):
            yield output[start:start + 96]
            await asyncio.sleep(0)

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        return {"compliant": False, "missing_sections": [],
                "rationale": "Placeholder implementation; medical verification not performed."}


class PlaceholderTableClassifier:
    @property
    def model_version(self) -> str:
        return MODEL_IDENTITIES["classifier"]["version"]

    def classify(self, table: ParsedTable) -> tuple[TableType, float]:
        return TableType.other, 0.0


class PlaceholderLayoutExtractor:
    """Decode registered source bytes without claiming document/table parsing.

    The artifact reader is selected by infrastructure composition. Its URI is
    an immutable registered artifact, never an arbitrary external download URL.
    """

    def __init__(self, artifacts: Artifacts):
        self.artifacts = artifacts

    async def extract(self, source_uri: str, *, pages: str | None = None) -> dict:
        if pages is not None:
            raise ValueError("placeholder extraction does not support page selection")
        payload = await self.artifacts.get(source_uri)
        text = payload.decode("utf-8", errors="replace")
        text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
        return {"content": text[:64000], "paragraphs": [], "tables": [],
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "implementation": PARSER_VERSION, "truncated": len(text) > 64000,
                "medical_parsing": False}

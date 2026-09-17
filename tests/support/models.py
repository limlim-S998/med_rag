"""Controlled model responses and a distinct embedding identity for offline tests."""

import hashlib
import math
from collections.abc import AsyncIterator


class HashEmbedder:
    """Satisfies medw_core.ports.Embedder."""

    def __init__(self, dimensions: int = 3072):
        self._dim = dimensions

    @property
    def embed_version(self) -> str:
        # Deliberately not v-anything: this string lands in the Qdrant
        # collection name, so a local index can never collide with a real one.
        return "local-hash-000"

    @property
    def dimensions(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in text.lower().split():
            h = hashlib.blake2b(token.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self._dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class ScriptedChatClient:
    """Satisfies medw_core.ports.ChatClient.

    `responses` is consumed in order; when exhausted it repeats the last one so
    a test that makes an unexpected extra call fails on the assertion rather
    than on an IndexError three frames away.
    """

    def __init__(self, responses: list[str] | None = None,
                 json_responses: list[dict] | None = None):
        self.responses = responses or ["connective prose only."]
        self.json_responses = json_responses or [{}]
        self.calls: list[str] = []          # every prompt seen, for assertions

    def _next(self, seq: list):
        return seq[min(len(self.calls) - 1, len(seq) - 1)]

    async def stream(self, prompt: str, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        self.calls.append(prompt)
        # Yields token-by-token so a consumer that assumes streaming is
        # exercised as streaming, not handed one lump.
        for word in self._next(self.responses).split():
            yield word + " "

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        self.calls.append(prompt)
        return self._next(self.json_responses)


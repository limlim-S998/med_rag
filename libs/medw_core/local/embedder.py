# A deterministic embedder with no network and no model.
#
# Hashing, not learning: each token is hashed into a bucket and the vector is
# L2-normalised. Nearby text does NOT get nearby vectors, so retrieval quality
# is meaningless - which is the point. It exists to make the pipeline runnable
# and the dimensions correct, not to retrieve well. Anything that reports a
# recall number against this backend is reporting nonsense, and the
# embed_version below says so out loud so it can never be confused with a real
# measurement or, worse, indexed into a collection alongside real vectors.

import hashlib
import math


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

# The Azure-side implementations of the shared ports.
#
# These live in medw_core rather than in a service because more than one
# service needs them: retrieval embeds queries, ingestion embeds chunks, and
# both must use the same deployment or the vectors are not comparable. A
# per-service copy is how that invariant gets broken.
#
# The store-specific adapters (QdrantRepo, SparseRepo) deliberately do NOT
# live here. They belong to the retrieval service, because a service that does
# no retrieval has no business carrying the Qdrant SDK — the same argument as
# ADR 0005, one layer down in the dependency graph.

from collections.abc import AsyncIterator

from medw_core.errors import QuotaExceeded
from medw_core.rate_limit import TokenBucket, with_backoff
from medw_core.settings import Settings


class AzureOpenAIEmbedder:
    """Satisfies medw_core.ports.Embedder.

    `embed_version` is NOT derived from the deployment name. It is set
    explicitly in settings and baked into the Qdrant collection name, because
    two different deployments of the same model still produce comparable
    vectors while a model change does not — so the version has to be a
    deliberate statement, not something inferred from a string.
    """

    def __init__(self, client, s: Settings, bucket: TokenBucket | None = None):
        self._client = client
        self._s = s
        # AOAI quota is per-deployment tokens-per-minute and is shared with the
        # generation path, so the limiter belongs on the client that spends it.
        self._bucket = bucket

    @property
    def embed_version(self) -> str:
        return self._s.embed_version

    @property
    def dimensions(self) -> int:
        return self._s.embed_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._bucket:
            # Rough token estimate: ~4 chars per token. Deliberately crude —
            # the bucket exists to keep us mostly under the limit, and
            # with_backoff handles the times it does not.
            await self._bucket.take(sum(len(t) for t in texts) // 4 + 1)

        async def call():
            return await self._client.embeddings.create(
                model=self._s.embed_deployment,   # DEPLOYMENT name, not model name
                input=texts,
            )

        try:
            r = await with_backoff(call)
        except Exception as e:
            raise QuotaExceeded("embedding quota exhausted after retries") from e
        return [d.embedding for d in r.data]


class AzureOpenAIChatClient:
    """Satisfies medw_core.ports.ChatClient.

    `stream` is a plain def returning an async generator, matching the port —
    see the note there. Declaring it `async def` would have made it a coroutine
    resolving to an iterator, which is not what an `async def ... yield`
    function is, and no implementation could have satisfied it.
    """

    def __init__(self, client, s: Settings, bucket: TokenBucket | None = None):
        self._client = client
        self._s = s
        self._bucket = bucket

    async def stream(self, prompt: str, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        if self._bucket:
            await self._bucket.take(len(prompt) // 4 + max_tokens)
        stream = await self._client.chat.completions.create(
            model=self._s.chat_deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,          # not creative writing. Pinned, not defaulted.
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        # The parse-validate-retry loop belongs here rather than at every call
        # site: on a validation failure, retry once with the error appended.
        # Cheap, and it removes most malformed-output incidents.
        ...

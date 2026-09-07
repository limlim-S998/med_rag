# The composition root. One place where implementations are chosen.
#
# Before this, each service built its dependencies inline in `lifespan()` into
# an untyped `ctx: dict` — four copies of the same wiring, 40 assignments, and
# no single answer to "what is this service actually talking to". Adding a
# local backend would have meant an `if` in four files, which is how a seam
# stops being a seam.
#
# The rule: **application code never constructs a dependency.** It receives one
# through a port. Only this module knows a concrete type exists, and only this
# module reads MEDW_BACKEND. If you find yourself importing `azure` or
# `qdrant_client` in a request handler, the boundary has already gone.
#
# Two backends:
#
#   azure  the real thing. Azure OpenAI, Cognitive Search, Cosmos, Azure SQL.
#   local  in-memory and on-disk stand-ins. No network, no credential, no cost.
#          Every port has one, which is the only reason the ports can be
#          trusted to be abstractions rather than the Azure SDK renamed.
#
# Qdrant is deliberately absent from that split: it is the real thing in both,
# because it runs in a container locally. A fake would be strictly worse than
# the genuine article.

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

from medw_core import ports
from medw_core.settings import Settings

Backend = Literal["azure", "local"]


@dataclass(frozen=True)
class Services:
    """Everything a service might need, already wired.

    Frozen: a service cannot swap a dependency at runtime. If it could, the
    composition root would no longer be the answer to "what is this talking
    to" — it would just be the answer at startup.

    Every field is a Protocol, never a concrete class. That is what stops a
    handler reaching past the port for `._client` and quietly coupling itself
    to the SDK.
    """

    backend: Backend
    embedder: ports.Embedder | None = None
    vectors: ports.VectorIndex | None = None
    sparse: ports.SparseIndex | None = None
    reranker: ports.Reranker | None = None
    chat: ports.ChatClient | None = None
    layout: ports.LayoutExtractor | None = None
    entities: ports.EntityExtractor | None = None
    classifier: ports.TableClassifier | None = None
    jobs: ports.JobStore | None = None
    sessions: ports.SessionStore | None = None
    documents: ports.DocumentStore | None = None
    audit: ports.AuditSink | None = None

    def require(self, name: str):
        """Fetch a dependency, failing loudly if this service was not given it.

        Services are wired with only what they need — retrieval gets no audit
        sink, the reranker gets nothing at all. Reaching for an absent
        dependency should say so plainly at the call site rather than raise
        AttributeError on None three frames down.
        """
        value = getattr(self, name, None)
        if value is None:
            raise RuntimeError(
                f"{name!r} was not wired for this service under backend "
                f"{self.backend!r}. Add it in medw_core.composition, not here."
            )
        return value


async def build(s: Settings, stack: AsyncExitStack, *, credential=None) -> Services:
    """Construct the dependency graph for `s.backend`.

    Takes an AsyncExitStack rather than owning cleanup: the caller's lifespan
    already has a scope, and two competing shutdown paths is how a credential
    gets closed while a request is still using it.

    `credential` exists because some services build their own store adapters
    (retrieval needs a SearchClient, the gateway a CosmosClient) and those need
    the same credential this function would otherwise create privately. Passing
    it in means one credential per process instead of two - and two is not
    merely wasteful, it is two token caches refreshing independently.
    """
    if s.backend == "local":
        return _build_local(s)
    return await _build_azure(s, stack, credential)


def _build_local(s: Settings) -> Services:
    # Imported here, not at module scope: the local package must never be a
    # runtime dependency of a production image.
    from medw_core.local.audit import InMemoryAuditSink
    from medw_core.local.chat import ScriptedChatClient
    from medw_core.local.embedder import HashEmbedder
    from medw_core.local.jobs import InMemoryJobStore
    from medw_core.local.stores import (
        DictionaryEntityExtractor,
        FixtureLayoutExtractor,
        InMemoryDocumentStore,
        InMemorySessionStore,
        InMemorySparseIndex,
    )

    return Services(
        backend="local",
        embedder=HashEmbedder(dimensions=s.embed_dim),
        sparse=InMemorySparseIndex(),
        chat=ScriptedChatClient(),
        layout=FixtureLayoutExtractor(s.fixture_dir),
        entities=DictionaryEntityExtractor(),
        jobs=InMemoryJobStore(),
        sessions=InMemorySessionStore(),
        documents=InMemoryDocumentStore(),
        audit=InMemoryAuditSink(),
        # vectors stays None: Qdrant is real in both backends, and a service
        # that needs it gets it from its own adapter. See the note at the top.
    )


async def _build_azure(s: Settings, stack: AsyncExitStack, credential=None) -> Services:
    from medw_core import azure
    from medw_core.adapters import AzureOpenAIChatClient, AzureOpenAIEmbedder
    from medw_core.cosmos import DocumentRepo, SessionRepo, containers
    from medw_core.rate_limit import TokenBucket

    cred = credential
    if cred is None:
        cred = azure.credential()
        await stack.enter_async_context(cred)

    # Only the clients themselves are built here. The adapters that wrap them
    # (QdrantRepo, SparseRepo) live with their services, because they are
    # infrastructure detail rather than shared vocabulary — and because a
    # service that does not do retrieval has no business importing them.
    aoai = azure.openai_client(s, cred)
    stack.push_async_callback(aoai.close)

    # One bucket shared by both AOAI adapters in this process: the quota is
    # per-deployment, so two independent limiters would each think they had
    # the whole allowance and together exceed it.
    bucket = TokenBucket(s.pod_tpm)

    # Cosmos-backed stores. These live in medw_core (unlike QdrantRepo and
    # SparseRepo) because two services need them: the gateway holds sessions,
    # the ingestion worker holds document metadata.
    cosmos = azure.cosmos_client(s, cred)
    stack.push_async_callback(cosmos.close)
    c = containers(cosmos, s)

    return Services(
        backend="azure",
        embedder=AzureOpenAIEmbedder(aoai, s, bucket),
        chat=AzureOpenAIChatClient(aoai, s, bucket),
        sessions=SessionRepo(c["sessions"]),
        documents=DocumentRepo(c["documents"]),
        # Store-specific adapters (QdrantRepo, SparseRepo) are attached by the
        # owning service with dataclasses.replace - they live in that service's
        # package, and medw_core must not import service code.
    )


# Why `Services` rather than passing a container into every function: the
# container is constructed once at startup and read many times, so the
# alternative — threading eight arguments through every call — buys purity at
# the cost of a signature change every time a dependency is added. The frozen
# dataclass keeps the graph explicit and greppable without that churn.

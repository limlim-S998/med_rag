"""Choose service-scoped dependencies once, at startup.

Qdrant and Search adapters remain service-owned factories so the shared
library never imports a service. Backend selection happens here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

from medw_core import ports
from medw_core.health import HealthMonitor, unavailable_check
from medw_core.settings import Settings


@dataclass(frozen=True)
class Services:
    backend: Literal["azure", "local"]
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
    state: ports.StateStore | None = None
    index_registry: ports.IndexSelector | None = None
    evidence: ports.EvidenceStore | None = None
    authorization: ports.StudyAccess | None = None
    health: ports.HealthCheck | None = None

    def require(self, name: str):
        value = getattr(self, name, None)
        if value is None:
            raise RuntimeError(f"{name!r} was not wired under {self.backend}")
        return value


DEPENDENCIES = {
    "gateway": {"sessions", "documents", "authorization"},
    "retrieval": {"embedder", "sparse", "vectors", "state", "index_registry"},
    "generation": {"chat", "audit", "evidence", "state"},
    "ingestion-worker": {"embedder", "jobs", "documents", "audit", "state",
                         "index_registry", "evidence"},
    "reranker": {"reranker"},
}


def effective_settings(s: Settings) -> Settings:
    """Synthetic model identities cannot be confused with Azure vector spaces."""
    if s.backend == "local":
        return s.model_copy(update={
            "chat_deployment": "local-scripted", "chat_model_name": "synthetic",
            "chat_model_version": "1", "embed_deployment": "local-hash-000",
            "embed_version": "local-hash-000", "embed_model_name": "synthetic-hash",
            "embed_model_version": "1", "table_classifier_version": "held-back",
        })
    return s


async def build(s: Settings, stack: AsyncExitStack, *, credential=None,
                service: str | None = None, vector_factory: Callable | None = None,
                sparse_factory: Callable | None = None) -> Services:
    from medw_core.durable_jobs import DurableJobStore
    from medw_core.indexing import IndexRegistry

    s = effective_settings(s)
    name = service or s.service_name
    required = DEPENDENCIES.get(name, set().union(*DEPENDENCIES.values()) | {"layout", "entities"})
    health = HealthMonitor(timeout=s.readiness_timeout, cache_seconds=s.readiness_cache_seconds)
    result: dict = {"backend": s.backend, "health": health}
    state: ports.StateStore | None = None

    if s.backend == "local":
        from medw_core.local.chat import ScriptedChatClient
        from medw_core.local.embedder import HashEmbedder
        from medw_core.local.indexes import DurableSparseIndex
        from medw_core.local.platform import (
            LocalStudyAccess,
            PersistentDocumentStore,
            PersistentSessionStore,
        )
        from medw_core.local.reranker import SyntheticReranker
        from medw_core.local.stores import (
            DictionaryEntityExtractor,
            FixtureLayoutExtractor,
        )
        from medw_core.persistence import SQLiteStateStore
        from medw_core.sources import EvidenceStore, LocalArtifacts

        if required & {"state", "jobs", "sessions", "documents", "authorization", "evidence"}:
            local_state = SQLiteStateStore(s.local_state_path)
            state = local_state
            stack.push_async_callback(local_state.close)
            health.add("local-state", local_state.check)
            result["state"] = state
        factories: dict[str, Callable[[], object]] = {
            "embedder": lambda: HashEmbedder(dimensions=s.embed_dim),
            "chat": ScriptedChatClient,
            "reranker": SyntheticReranker,
            "layout": lambda: FixtureLayoutExtractor(s.fixture_dir),
            "entities": DictionaryEntityExtractor,
        }
        for field, factory in factories.items():
            if field in required:
                result[field] = factory()
        if state is not None:
            if "sparse" in required:
                result["sparse"] = DurableSparseIndex(state)
            if "sessions" in required:
                result["sessions"] = PersistentSessionStore(state)
            if "documents" in required:
                result["documents"] = PersistentDocumentStore(state)
            if "authorization" in required:
                result["authorization"] = LocalStudyAccess(state)
        if "audit" in required:
            from medw_core.local.durable_audit import SQLiteAuditSink
            audit = SQLiteAuditSink(s.local_state_path)
            result["audit"] = audit
            stack.push_async_callback(audit.close)
            health.add("audit", audit.check)
        if "evidence" in required:
            assert state is not None
            result["evidence"] = EvidenceStore(state, LocalArtifacts(s.local_artifact_dir))
    else:
        if name == "reranker":
            health.add("reranker-model", unavailable_check("model implementation held back"))
            return Services(**result)
        from medw_core import azure
        from medw_core.adapters import AzureOpenAIChatClient, AzureOpenAIEmbedder
        from medw_core.rate_limit import TokenBucket

        cred = credential or azure.credential()
        if credential is None:
            await stack.enter_async_context(cred)
        if required & {"embedder", "chat"}:
            import httpx

            from medw_core.model_identity import ModelIdentityCheck
            client = azure.openai_client(s, cred)
            stack.push_async_callback(client.close)
            # Chat and embeddings are different Azure deployments and quotas.
            if "embedder" in required:
                result["embedder"] = AzureOpenAIEmbedder(client, s, TokenBucket(s.embed_pod_tpm))
            if "chat" in required:
                result["chat"] = AzureOpenAIChatClient(client, s, TokenBucket(s.pod_tpm))

            async def check_openai():
                await client.models.list()
            health.add("azure-openai", check_openai)
            metadata_http = await stack.enter_async_context(httpx.AsyncClient(
                timeout=s.readiness_timeout))
            expected = []
            if "embedder" in required:
                expected.append((s.embed_deployment, s.embed_model_name, s.embed_model_version))
            if "chat" in required:
                expected.append((s.chat_deployment, s.chat_model_name, s.chat_model_version))
            health.add("model-identity", ModelIdentityCheck(
                cred, metadata_http, s.aoai_resource_id, expected).check)
        if required & {"sessions", "documents", "state", "jobs", "index_registry"}:
            from medw_core.cosmos import DocumentRepo, SessionRepo, containers
            from medw_core.cosmos_state import CosmosStateStore
            cosmos = azure.cosmos_client(s, cred)
            stack.push_async_callback(cosmos.close)
            boxes = containers(cosmos, s)
            if "sessions" in required:
                result["sessions"] = SessionRepo(boxes["sessions"])
                health.add("sessions", boxes["sessions"].read)
            if "documents" in required:
                result["documents"] = DocumentRepo(boxes["documents"])
                health.add("documents", boxes["documents"].read)
            if required & {"state", "jobs", "index_registry"}:
                state_container = cosmos.get_database_client(s.cosmos_database).get_container_client(
                    s.cosmos_state_container)
                cosmos_state = CosmosStateStore(state_container)
                state = cosmos_state
                result["state"] = state
                health.add("platform-state", cosmos_state.check)
        if required & {"audit", "authorization"}:
            from medw_core.sql import SqlAuditSink, SqlStudyAccess, engine
            database = engine(s, credential=cred)
            stack.push_async_callback(database.dispose)
            if "audit" in required:
                result["audit"] = SqlAuditSink(database)
            if "authorization" in required:
                result["authorization"] = SqlStudyAccess(database)

            async def check_sql():
                from sqlalchemy import text
                async with database.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            health.add("sql", check_sql)
        if "evidence" in required:
            from medw_core.blob_artifacts import BlobArtifacts
            from medw_core.sources import EvidenceStore
            blobs = azure.blob_client(s, cred)
            stack.push_async_callback(blobs.close)
            blob_container = blobs.get_container_client(s.blob_container)
            assert state is not None
            result["evidence"] = EvidenceStore(state, BlobArtifacts(blob_container))
            health.add("source-artifacts", blob_container.get_container_properties)
        if "sparse" in required and sparse_factory is not None:
            search = azure.search_client(s, cred)
            stack.push_async_callback(search.close)
            result["sparse"] = sparse_factory(search)
            health.add("sparse-index", search.get_document_count)
        if "reranker" in required:
            health.add("reranker-model", unavailable_check("model implementation held back"))

    if state is not None:
        if "jobs" in required:
            result["jobs"] = DurableJobStore(state)
        if "index_registry" in required:
            result["index_registry"] = IndexRegistry(state)
    if result.get("audit") is not None and result.get("evidence") is not None:
        result["audit"].evidence = result["evidence"]
    if "vectors" in required and vector_factory is not None:
        vectors = vector_factory(s)
        result["vectors"] = vectors
        stack.push_async_callback(vectors.client.close)
        health.add("vector-index", vectors.client.get_collections)
    # Service factories may be attached later by an owning service. A missing
    # required dependency is never treated as proof of readiness.
    if name in DEPENDENCIES:
        for field in required - result.keys():
            if field == "reranker" and s.backend == "azure":
                continue
            health.add(field, unavailable_check(f"{field} was not wired"))
    return Services(**result)


def readiness(services: Services, required: tuple[str, ...]) -> tuple[bool, str]:
    """Legacy structural check; live service probes await services.health.check."""
    missing = [name for name in required if getattr(services, name, None) is None]
    if missing:
        return False, "not wired: " + ", ".join(missing)
    if services.backend == "local":
        return True, "local backend: structurally wired"
    return False, "reachability is not established by wiring; await the health check"

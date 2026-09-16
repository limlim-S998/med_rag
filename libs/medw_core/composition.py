"""Select infrastructure once; both backends run the same model implementations."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

from medw_core import ports
from medw_core.health import HealthMonitor, unavailable_check
from medw_core.settings import Settings, require_setting


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
    drafts: ports.DraftStore | None = None
    uploads: ports.UploadStore | None = None
    dense_writer: ports.GenerationSink | None = None
    sparse_writer: ports.GenerationSink | None = None
    health: ports.HealthCheck | None = None

    def require(self, name: str):
        value = getattr(self, name, None)
        if value is None:
            raise RuntimeError(f"{name!r} was not wired under {self.backend}")
        return value


DEPENDENCIES = {
    "gateway": {"sessions", "documents", "authorization", "drafts", "uploads"},
    "retrieval": {"embedder", "sparse", "vectors", "state", "index_registry"},
    "generation": {"chat", "audit", "evidence", "state", "drafts", "authorization"},
    "ingestion-worker": {"embedder", "layout", "classifier", "jobs", "documents", "audit", "state",
                         "index_registry", "evidence", "uploads", "dense_writer", "sparse_writer"},
    "reranker": {"reranker"},
}


def effective_settings(s: Settings) -> Settings:
    """Model identity is explicit configuration, independent of infrastructure."""
    return s


async def build(s: Settings, stack: AsyncExitStack, *, credential=None,
                service: str | None = None, vector_factory: Callable | None = None,
                sparse_factory: Callable | None = None) -> Services:
    from medw_core.durable_jobs import DurableJobStore
    from medw_core.indexing import IndexRegistry
    from medw_core.placeholders import (
        PARSER_VERSION,
        HashEmbedder,
        PlaceholderChatClient,
        PlaceholderLayoutExtractor,
        PlaceholderReranker,
        PlaceholderTableClassifier,
        validate_model_identities,
    )
    from medw_core.uploads import UploadService, UploadStorage

    name = service or s.service_name
    required = DEPENDENCIES.get(name, set().union(*DEPENDENCIES.values()))
    health = HealthMonitor(timeout=s.readiness_timeout, cache_seconds=s.readiness_cache_seconds)
    result: dict = {"backend": s.backend, "health": health}
    state: ports.StateStore | None = None
    upload_state: ports.StateStore | None = None
    upload_storage: UploadStorage | None = None
    factories: dict[str, Callable[[], object]] = {
        "embedder": lambda: HashEmbedder(dimensions=s.embed_dim),
        "chat": PlaceholderChatClient, "reranker": PlaceholderReranker,
        "classifier": PlaceholderTableClassifier,
    }
    for field, factory in factories.items():
        if field in required:
            result[field] = factory()

    async def check_models():
        validate_model_identities(
            (s.chat_deployment, s.chat_model_name, s.chat_model_version),
            (s.embed_deployment, s.embed_model_name, s.embed_model_version))
        if (s.embed_version != "hash-1" or s.table_classifier_version != "placeholder-1"
                or s.parser_version != PARSER_VERSION):
            raise ValueError("configured model compatibility differs from installed implementation")

    health.add("installed-models", check_models)
    if "reranker" in required:
        health.add("reranker-model", result["reranker"].check)

    if s.backend == "local":
        from medw_core.local.indexes import DurableSparseIndex, SQLiteGenerationSink
        from medw_core.local.platform import (
            LocalStudyAccess,
            PersistentDocumentStore,
            PersistentSessionStore,
        )
        from medw_core.persistence import SQLiteStateStore
        from medw_core.sources import EvidenceStore, LocalArtifacts
        from medw_core.uploads import LocalUploadStorage

        if required & {"state", "jobs", "sessions", "documents", "authorization", "evidence", "uploads"}:
            state = local_state = SQLiteStateStore(s.local_state_path)
            stack.push_async_callback(local_state.close)
            health.add("local-state", local_state.check)
            result["state"] = state
            upload_state = state
            local_factories: dict[str, Callable[[], object]] = {
                "sparse": lambda: DurableSparseIndex(local_state),
                "sparse_writer": lambda: SQLiteGenerationSink(local_state),
                "sessions": lambda: PersistentSessionStore(local_state),
                "documents": lambda: PersistentDocumentStore(local_state),
                "authorization": lambda: LocalStudyAccess(local_state),
                "evidence": lambda: EvidenceStore(local_state, LocalArtifacts(s.local_artifact_dir)),
            }
            for field, factory in local_factories.items():
                if field in required:
                    result[field] = factory()
        if "uploads" in required:
            from pathlib import Path
            upload_storage = LocalUploadStorage(Path(s.local_artifact_dir) / "staging")
        if "audit" in required:
            from medw_core.local.durable_audit import SQLiteAuditSink
            audit = SQLiteAuditSink(s.local_state_path)
            result["audit"] = audit
            stack.push_async_callback(audit.close)
            health.add("audit", audit.check)
        if "drafts" in required:
            from medw_core.drafts import SQLiteDraftStore
            drafts = SQLiteDraftStore(s.local_state_path)
            result["drafts"] = drafts
            stack.push_async_callback(drafts.close)
            health.add("drafts", drafts.check)
    elif required - {"reranker"}:
        from medw_core import azure

        cred = credential or azure.credential()
        if credential is None:
            await stack.enter_async_context(cred)
        if required & {"sessions", "documents", "state", "jobs", "index_registry", "uploads"}:
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
            if "uploads" in required:
                upload_state = CosmosStateStore(boxes["documents"])
            if required & {"state", "jobs", "index_registry"}:
                container = cosmos.get_database_client(s.cosmos_database).get_container_client(
                    require_setting(s.cosmos_state_container, "MEDW_COSMOS_STATE_CONTAINER"))
                state = cosmos_state = CosmosStateStore(container)
                result["state"] = state
                health.add("platform-state", cosmos_state.check)
        if required & {"audit", "authorization", "drafts"}:
            from medw_core.drafts import SqlDraftStore
            from medw_core.sql import SqlAuditSink, SqlStudyAccess, engine
            database = engine(s, credential=cred)
            stack.push_async_callback(database.dispose)
            if "audit" in required:
                result["audit"] = SqlAuditSink(database)
            if "authorization" in required:
                result["authorization"] = SqlStudyAccess(database)
            if "drafts" in required:
                result["drafts"] = SqlDraftStore(database)
                health.add("drafts", result["drafts"].check)

            async def check_sql():
                from sqlalchemy import text
                async with database.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            health.add("sql", check_sql)
        if required & {"evidence", "uploads"}:
            from medw_core.blob_artifacts import BlobArtifacts
            from medw_core.sources import EvidenceStore
            from medw_core.uploads import AzureUploadStorage
            blobs = azure.blob_client(s, cred)
            stack.push_async_callback(blobs.close)
            container_name = require_setting(s.blob_container, "MEDW_BLOB_CONTAINER")
            blob_container = blobs.get_container_client(container_name)
            if "evidence" in required:
                assert state is not None
                result["evidence"] = EvidenceStore(state, BlobArtifacts(blob_container))
            if "uploads" in required:
                upload_storage = AzureUploadStorage(blobs, container_name)
            health.add("source-artifacts", blob_container.get_container_properties)
        if required & {"sparse", "sparse_writer"}:
            search = azure.search_client(s, cred)
            stack.push_async_callback(search.close)
            if "sparse" in required and sparse_factory is not None:
                result["sparse"] = sparse_factory(search)
            if "sparse_writer" in required:
                from medw_core.search_sink import SearchGenerationSink
                result["sparse_writer"] = SearchGenerationSink(search)
            health.add("sparse-index", search.get_document_count)

    if state is not None:
        if "jobs" in required:
            result["jobs"] = DurableJobStore(state)
        if "index_registry" in required:
            result["index_registry"] = IndexRegistry(state)
    if "uploads" in required:
        assert upload_state is not None and upload_storage is not None
        result["uploads"] = UploadService(upload_state, upload_storage,
                                         ttl_seconds=s.upload_ttl_seconds, max_bytes=s.upload_max_bytes)
    if "layout" in required:
        result["layout"] = PlaceholderLayoutExtractor(result["evidence"].artifacts)
    if result.get("audit") is not None and result.get("evidence") is not None:
        result["audit"].evidence = result["evidence"]
    if "vectors" in required and vector_factory is not None:
        vectors = vector_factory(s)
        result["vectors"] = vectors
        stack.push_async_callback(vectors.client.close)
        health.add("vector-index", vectors.client.get_collections)
    if "dense_writer" in required:
        from qdrant_client import AsyncQdrantClient

        from medw_core.qdrant_sink import QdrantGenerationSink
        client = AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None,
                                   timeout=max(1, int(s.readiness_timeout)))
        stack.push_async_callback(client.close)
        result["dense_writer"] = QdrantGenerationSink(
            client, replication_factor=s.qdrant_replication_factor,
            write_consistency_factor=s.qdrant_write_consistency_factor,
            shard_number=s.qdrant_shard_number)
        health.add("dense-writer", client.get_collections)
    if name in DEPENDENCIES:
        for field in required - result.keys():
            health.add(field, unavailable_check(f"{field} was not wired"))
    return Services(**result)


def readiness(services: Services, required: tuple[str, ...]) -> tuple[bool, str]:
    """Structural check only; HTTP readiness also probes live dependencies."""
    missing = [name for name in required if getattr(services, name, None) is None]
    if missing:
        return False, "not wired: " + ", ".join(missing)
    if services.backend == "local":
        return True, "local backend: structurally wired"
    return False, "reachability is not established by wiring; await the health check"

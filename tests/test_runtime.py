"""Service isolation, dependency failure/recovery and offline session persistence."""

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from pydantic import ValidationError
from support.state import SQLiteStateStore
from support.stores import PersistentSessionStore

from medw_core.composition import DEPENDENCIES, build
from medw_core.health import DependencyUnavailable, HealthMonitor, http_check
from medw_core.settings import Settings


@pytest.mark.parametrize("service", list(DEPENDENCIES))
async def test_azure_dependencies_are_scoped_probed_and_closed_offline(service, monkeypatch):
    """Exercise the real composition root, replacing only external client boundaries."""
    from unittest.mock import AsyncMock, MagicMock, Mock

    import qdrant_client

    from medw_core import azure, sql
    from services.retrieval.app import qdrant_repo
    from services.retrieval.app.sparse_repo import SparseRepo

    container = Mock(read=AsyncMock(), get_container_properties=AsyncMock())
    cosmos = Mock(close=AsyncMock())
    cosmos.get_database_client.return_value.get_container_client.return_value = container
    blobs = Mock(close=AsyncMock())
    blobs.get_container_client.return_value = container
    search = Mock(close=AsyncMock(), get_document_count=AsyncMock(return_value=0))
    vectors = Mock(close=AsyncMock(), get_collections=AsyncMock())
    database = Mock(dispose=AsyncMock())
    connection = MagicMock()
    connection.__aenter__.return_value = Mock(execute=AsyncMock())
    database.connect.return_value = connection
    factories = {}
    for module, name, value in [(azure, "cosmos_client", cosmos),
                                (azure, "blob_client", blobs),
                                (azure, "search_client", search),
                                (sql, "engine", database)]:
        factories[name] = Mock(return_value=value)
        monkeypatch.setattr(module, name, factories[name])
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", Mock(return_value=vectors))
    monkeypatch.setattr(qdrant_repo, "AsyncQdrantClient", Mock(return_value=vectors))
    monkeypatch.setattr(azure, "openai_client", Mock(side_effect=AssertionError("unused model")))
    settings = Settings(_env_file=None, env="test", readiness_cache_seconds=0)
    required = DEPENDENCIES[service]
    async with AsyncExitStack() as stack:
        services = await build(settings, stack, service=service, credential=object(),
                               vector_factory=qdrant_repo.QdrantRepo, sparse_factory=SparseRepo)
        for name in required:
            assert services.require(name) is not None
        for name in set().union(*DEPENDENCIES.values()) - required:
            assert getattr(services, name) is None
        await services.health.check()
        if service != "reranker":
            container.read.side_effect = ConnectionError("Cosmos unavailable")
            with pytest.raises(DependencyUnavailable):
                await services.health.check()
            container.read.side_effect = None
            if services.evidence or services.uploads:
                container.get_container_properties.side_effect = ConnectionError("Blob unavailable")
                with pytest.raises(DependencyUnavailable, match="source-artifacts"):
                    await services.health.check()
                container.get_container_properties.side_effect = None
            await services.health.check()
    expectations = {
        "cosmos_client": service != "reranker",
        "blob_client": service in {"gateway", "generation", "ingestion-worker"},
        "search_client": service in {"retrieval", "ingestion-worker"},
        "engine": service in {"gateway", "generation", "ingestion-worker"},
    }
    for name, expected in expectations.items():
        assert factories[name].call_count == int(expected)
        resource = factories[name].return_value
        closer = resource.dispose if name == "engine" else resource.close
        assert closer.await_count == int(expected)
    assert vectors.close.await_count == int(service in {"retrieval", "ingestion-worker"})


async def test_probe_recovers_without_rebuilding_service():
    failed = True
    health = HealthMonitor(timeout=0.05, cache_seconds=0)

    async def dependency():
        if failed:
            raise ConnectionError("dependency offline")

    health.add("store", dependency)
    with pytest.raises(DependencyUnavailable, match="store"):
        await health.check()
    failed = False
    await health.check()


async def test_probe_timeout_is_bounded_and_http_status_is_checked():
    health = HealthMonitor(timeout=0.01, cache_seconds=0)
    health.add("hung", lambda: asyncio.sleep(10))
    with pytest.raises(DependencyUnavailable, match="hung"):
        await asyncio.wait_for(health.check(), timeout=0.5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(503),
    )) as client:
        health = HealthMonitor(cache_seconds=0)
        health.add("reranker", http_check(client, "https://test/readyz"))
        with pytest.raises(DependencyUnavailable, match="reranker"):
            await health.check()


async def test_azure_reranker_live_shell_never_constructs_an_azure_credential(monkeypatch):
    from medw_core import azure

    def forbidden():
        pytest.fail("reranker must not create Azure credentials")

    monkeypatch.setattr(azure, "credential", forbidden)
    async with AsyncExitStack() as stack:
        services = await build(Settings(_env_file=None), stack, service="reranker")
        assert services.reranker is not None
        await services.health.check()
        assert await services.reranker.rerank("hello", [("1", "hello")], top_k=1) == [("1", 1.0)]


async def test_session_persists_but_user_scope_and_expiry_are_enforced(tmp_path):
    path = tmp_path / "sessions.db"
    state = SQLiteStateStore(path)
    await PersistentSessionStore(state).put({"session_id": "session", "user_id": "user"})
    await state.close()
    state = SQLiteStateStore(path)
    store = PersistentSessionStore(state)
    assert await store.get("user", "session") == {"session_id": "session", "user_id": "user"}
    assert await store.get("someone-else", "session") is None
    await PersistentSessionStore(state, ttl_seconds=-1).put(
        {"session_id": "expired", "user_id": "user"})
    assert await store.get("user", "expired") is None
    await state.close()


def test_insecure_jwks_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_jwks_url="http://identity.test/keys")


async def test_retrieval_selects_one_generation_for_both_searches(monkeypatch):
    from types import SimpleNamespace

    from medw_core.indexing import make_generation
    from medw_core.schemas import RetrievalRequest
    from services.retrieval.app import main

    settings = Settings(_env_file=None, env="test")
    monkeypatch.setattr(main, "s", settings)
    generation = make_generation(
        "study", [], parser_version="synthetic-1", embed_version=settings.embed_version,
        embed_deployment=settings.embed_deployment, embed_model_name=settings.embed_model_name,
        embed_model_version=settings.embed_model_version, dimensions=settings.embed_dim,
    )
    selected, filters = [], []

    async def select(study_id, **identity):
        selected.append((study_id, identity))
        return generation

    async def embed(text):
        return [[0.0] * settings.embed_dim]

    async def search(query, flt, *, limit):
        filters.append(flt)
        return []

    dependencies = {
        "index_registry": SimpleNamespace(select=select),
        "embedder": SimpleNamespace(embed=embed, embed_version=settings.embed_version,
                                    dimensions=settings.embed_dim),
        "vectors": SimpleNamespace(search=search), "sparse": SimpleNamespace(search=search),
    }
    actual, dense, sparse = await main.retrieve_candidates(
        SimpleNamespace(require=dependencies.__getitem__),
        RetrievalRequest(study_id="study", query="synthetic evidence"),
    )
    assert actual == generation and dense == sparse == []
    assert len(selected) == 1 and len(filters) == 2
    assert filters[0] is filters[1]
    assert filters[0].index_generation == generation
    assert selected[0][1]["embed_model_name"] == settings.embed_model_name

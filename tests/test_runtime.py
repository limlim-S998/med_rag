"""Service isolation, dependency failure/recovery and durable local sessions."""

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from pydantic import ValidationError

from medw_core.composition import DEPENDENCIES, build
from medw_core.health import DependencyUnavailable, HealthMonitor, http_check
from medw_core.local.platform import PersistentSessionStore
from medw_core.persistence import SQLiteStateStore
from medw_core.settings import Settings


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


@pytest.mark.parametrize("service", ["gateway", "generation", "ingestion-worker", "reranker"])
async def test_local_service_dependencies_are_scoped(service, tmp_path):
    settings = Settings(backend="local", env="test", local_state_path=str(tmp_path / "state.db"),
                        local_artifact_dir=str(tmp_path / "artifacts"))
    async with AsyncExitStack() as stack:
        services = await build(settings, stack, service=service)
        await services.health.check()
        for field in DEPENDENCIES[service]:
            assert services.require(field) is not None
        if service in {"gateway", "reranker"}:
            assert services.embedder is None and services.chat is None
        if service == "reranker":
            assert services.audit is None and services.state is None


async def test_azure_reranker_live_shell_never_constructs_an_azure_credential(monkeypatch):
    from medw_core import azure

    def forbidden():
        pytest.fail("reranker must not create Azure credentials")

    monkeypatch.setattr(azure, "credential", forbidden)
    async with AsyncExitStack() as stack:
        services = await build(Settings(backend="azure"), stack, service="reranker")
        assert services.reranker is None
        with pytest.raises(DependencyUnavailable, match="reranker-model"):
            await services.health.check()


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


@pytest.mark.parametrize("values", [
    {"backend": "typo"}, {"backend": "local", "env": "prod"},
    {"backend": "azure", "env": "local", "synthetic_enabled": True},
    {"backend": "local", "env": "dev", "synthetic_enabled": True},
])
def test_invalid_modes_are_rejected(values):
    with pytest.raises(ValidationError):
        Settings(**values)


async def test_retrieval_selects_one_generation_for_both_searches(monkeypatch):
    from types import SimpleNamespace

    from medw_core.composition import effective_settings
    from medw_core.indexing import make_generation
    from medw_core.schemas import RetrievalRequest
    from services.retrieval.app import main

    settings = effective_settings(Settings(backend="local", env="test"))
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

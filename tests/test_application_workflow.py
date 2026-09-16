"""Exercise the normal HTTP workflow with durable data and signed identities."""

import json
import sqlite3
import time
import uuid
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient

from medw_core import tracing
from medw_core.auth import TokenValidator
from medw_core.composition import Services
from medw_core.drafts import SQLiteDraftStore
from medw_core.ids import chunk_id
from medw_core.indexing import IndexRegistry, make_generation, publish_generation
from medw_core.local.durable_audit import SQLiteAuditSink
from medw_core.local.indexes import DurableSparseIndex, SQLiteGenerationSink
from medw_core.local.platform import LocalStudyAccess
from medw_core.persistence import Conflict, SQLiteStateStore
from medw_core.placeholders import HashEmbedder, PlaceholderChatClient, PlaceholderReranker
from medw_core.provenance import Provenance
from medw_core.qdrant_sink import QdrantGenerationSink
from medw_core.schemas import Chunk, DocType
from medw_core.settings import Settings
from medw_core.sources import EvidenceStore, LocalArtifacts
from services.gateway.app.routes.draft import router as acceptance_router
from services.generation.app import main as generation
from services.reranker.app import main as reranker
from services.retrieval.app import main as retrieval
from services.retrieval.app.fusion import rrf
from services.retrieval.app.qdrant_repo import QdrantRepo


@pytest.fixture
async def workflow(tmp_path, monkeypatch):
    async with AsyncExitStack() as stack:
        path = tmp_path / "state.sqlite3"
        state = SQLiteStateStore(path)
        stack.push_async_callback(state.close)
        evidence = EvidenceStore(state, LocalArtifacts(tmp_path / "artifacts"))
        payload = b"Evidence from uploaded document: blue flowers and 12 birds."
        source = await evidence.ingest_source("S1", "doc1", payload, "evidence.txt")
        chunk = Chunk(
            id=chunk_id("S1", "doc1", "1", 0, source_revision=source.revision_id),
            study_id="S1", doc_id="doc1", doc_type=DocType.protocol,
            section_path="1", kind="prose", text=payload.decode(), ordinal=0,
            source_revision=source.revision_id, parser_version="p7", source_location="bytes 0-57")
        await evidence.archive_chunk(chunk)
        embedder = HashEmbedder(dimensions=64)
        manifest = make_generation("S1", [chunk], parser_version="p7", embed_version="hash-1",
                                   embed_deployment="hash-1", embed_model_name="token-hash",
                                   embed_model_version="1", dimensions=64)
        qdrant = AsyncQdrantClient(location=":memory:")
        stack.push_async_callback(qdrant.close)
        registry = IndexRegistry(state)

        async def evaluated(manifest):
            return True

        await publish_generation(registry, manifest, [chunk], await embedder.embed([chunk.text]),
                                 dense=QdrantGenerationSink(qdrant),
                                 sparse=SQLiteGenerationSink(state), evaluate=evaluated,
                                 expected_revision=None, evidence=evidence)
        repo = object.__new__(QdrantRepo)
        repo.s = SimpleNamespace(search_ef=16)
        repo.client = qdrant
        access = LocalStudyAccess(state)
        await access.grant("writer", "S1")
        await access.grant("other-writer", "S1")
        audit = SQLiteAuditSink(path, evidence)
        drafts = SQLiteDraftStore(path)
        stack.push_async_callback(audit.close)
        stack.push_async_callback(drafts.close)
        monkeypatch.setattr(retrieval, "s", SimpleNamespace(
            embed_deployment="hash-1", embed_model_version="1", embed_model_name="token-hash",
            fusion_top_n=20, rrf_k=60, reranker_url="http://reranker"))
        monkeypatch.setattr(generation, "s", SimpleNamespace(retrieval_url="http://retrieval"))
        gateway = FastAPI()
        gateway.include_router(acceptance_router)
        gateway.add_middleware(tracing.TraceMiddleware, service="gateway")
        gateway.state.services = Services(backend="local", drafts=drafts, authorization=access)
        retrieval.app.state.services = Services(backend="local", embedder=embedder, vectors=repo,
                                                sparse=DurableSparseIndex(state), index_registry=registry)
        reranker.app.state.services = Services(backend="local", reranker=PlaceholderReranker())
        generation.app.state.services = Services(backend="local", evidence=evidence, audit=audit,
                                                 drafts=drafts, authorization=access,
                                                 chat=PlaceholderChatClient())
        generation.app.state.provenance = Provenance(
            "test", "generation", "a" * 40, "scripted-chat", "hash-1", "b" * 64,
            "placeholder-1", chat_model_name="scripted-placeholder", chat_model_version="1")
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        public.update(kid="test", alg="RS256", use="sig")
        transports = {name: httpx.ASGITransport(app=app) for name, app in
                      {"generation": generation.app, "retrieval": retrieval.app,
                       "reranker": reranker.app, "gateway": gateway}.items()}
        calls = []

        async def dispatch(request):
            calls.append((request.url.host, request.url.path, dict(request.headers)))
            if request.url.host == "identity":
                return httpx.Response(200, json={"keys": [public]})
            return await transports[request.url.host].handle_async_request(request)

        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            event_hooks={"request": [tracing.httpx_request_hook]}))
        config = Settings(backend="local", env="test", auth_tenant_id="tenant",
                          auth_audience="app", auth_issuer="https://identity/issuer",
                          auth_jwks_url="https://identity/keys")
        validator = TokenValidator(config, http)
        generation.app.state.http = retrieval.app.state.http = http
        generation.app.state.token_validator = gateway.state.token_validator = validator

        def authorization(oid="writer"):
            now = int(time.time())
            token = jwt.encode({"iss": config.auth_issuer, "aud": "app", "tid": "tenant",
                                "oid": oid, "iat": now, "nbf": now - 1, "exp": now + 60},
                               private, algorithm="RS256", headers={"kid": "test"})
            return {"Authorization": "Bearer " + token}

        yield SimpleNamespace(http=http, path=path, drafts=drafts, audit=audit, state=state,
                              source=source, manifest=manifest, authorization=authorization,
                              calls=calls, access=access, chunk=chunk)


async def generate(flow, **body):
    return await flow.http.post("http://generation/studies/S1/sections/1/draft",
                                json={"query": "blue flowers", **body},
                                headers=flow.authorization())


async def test_end_to_end_search_rerank_stream_audit_and_specific_acceptance(workflow):
    flow = workflow
    public = await flow.http.post("http://retrieval/studies/S1/search", json={"query": "blue"})
    assert public.status_code == 200
    assert public.json()["hits"][0]["citation"]["source_revision"] == flow.source.revision_id
    response = await generate(flow)
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "start" and events[-1]["type"] == "complete"
    assert sum(event["type"] == "delta" for event in events) > 1
    output = "".join(event["text"] for event in events if event["type"] == "delta")
    complete = events[-1]
    assert flow.chunk.text in output and flow.source.revision_id in output
    assert complete["verification"]["status"] == "not_performed"
    row = json.loads(flow.audit.connection.execute(
        "SELECT event_json FROM platform_audit WHERE event_id=?", (complete["event_id"],)
    ).fetchone()[0])
    assert row["user_oid"] == "writer" and row["output_text"] == output
    assert row["numeric_check_passed"] is False and row["structural_check_passed"] is False
    assert row["index_generation_id"] == flow.manifest.generation_id
    correlation = response.headers["x-correlation-id"]
    assert row["correlation_id"] == correlation
    assert any(host == "retrieval" and path == "/search" for host, path, _ in flow.calls)
    assert any(host == "reranker" and path == "/rerank" and headers["x-correlation-id"] == correlation
               for host, path, headers in flow.calls)
    body = {"draft_id": complete["draft_id"]}
    url = "http://gateway/studies/S1/sections/1/accept"
    for _ in range(2):
        accepted = await flow.http.post(url, json=body, headers=flow.authorization())
        assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    assert flow.drafts.connection.execute("SELECT count(*) FROM draft_acceptance").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        flow.drafts.connection.execute("DELETE FROM draft_acceptance")
    assert (await flow.http.post(url, json=body,
                                headers=flow.authorization("other-writer"))).status_code == 409
    wrong_section = url.replace("/sections/1/", "/sections/2/")
    assert (await flow.http.post(wrong_section, json=body,
                                headers=flow.authorization())).status_code == 404


async def test_generation_requires_verified_actor_membership_and_rejects_spoof_body(workflow):
    flow = workflow
    endpoint = "http://generation/studies/S1/sections/1/draft"
    assert (await flow.http.post(endpoint, json={"query": "blue"})).status_code == 401
    assert (await flow.http.post(endpoint.replace("/S1/", "/other/"), json={"query": "blue"},
                                headers=flow.authorization())).status_code == 403
    assert (await generate(flow, user_oid="attacker")).status_code == 422
    spoofed = await flow.http.post("http://retrieval/studies/S1/search",
                                  json={"query": "blue", "study_id": "other"})
    assert spoofed.status_code == 422
    await flow.access.revoke("writer", "S1")
    assert (await generate(flow)).status_code == 403


async def test_persistence_failure_emits_error_without_successful_draft(workflow, monkeypatch):
    async def unavailable(event):
        raise ConnectionError("SQL unavailable")

    monkeypatch.setattr(workflow.audit, "record_generation", unavailable)
    response = await generate(workflow)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error" and all(event["type"] != "complete" for event in events)
    assert await workflow.drafts.get("S1", "1", events[0]["draft_id"]) is None


async def test_index_audit_duplicate_is_idempotent_but_conflicting_payload_rejected(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.sqlite")
    event = {"event_id": str(uuid.uuid4()), "study_id": "S1", "doc_id": "doc1",
             "parser_version": "placeholder-text-1", "embed_version": "hash-1",
             "collection": "study_generation", "chunks_upserted": 1,
             "index_generation_id": "generation", "source_revision": "revision",
             "correlation_id": "retry-trace"}
    try:
        assert await sink.record_index(event) == await sink.record_index(event) == event["event_id"]
        assert sink.connection.execute("SELECT count(*) FROM platform_audit").fetchone()[0] == 1
        with pytest.raises(Conflict, match="different content"):
            await sink.record_index({**event, "chunks_upserted": 2})
    finally:
        await sink.close()


def test_rrf_does_not_compare_incompatible_scores_and_rejects_bad_parameter():
    assert [identifier for identifier, _ in rrf([["a", "b"], ["b", "c"]])] == ["b", "a", "c"]
    assert rrf([["a", "a"]]) == [("a", 1 / 61)]
    with pytest.raises(ValueError):
        rrf([["a"]], k=0)


async def test_generation_rejects_changed_index_text_even_when_citation_id_is_valid(workflow, monkeypatch):
    response = await workflow.http.post("http://retrieval/studies/S1/search", json={"query": "blue"})
    payload = response.json()
    payload["hits"][0]["text"] = "Tampered text carrying somebody else's valid citation ID"

    async def changed_index(*args, **kwargs):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://retrieval/search"))

    monkeypatch.setattr(generation.app.state, "http", SimpleNamespace(post=changed_index))
    result = await generate(workflow)
    assert result.status_code == 503
    assert workflow.audit.connection.execute("SELECT count(*) FROM platform_audit").fetchone()[0] == 0


async def test_sql_index_retry_tolerates_sql_server_uuid_readback_case():
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import IntegrityError

    from medw_core.sql import SqlAuditSink

    event = {"event_id": str(uuid.uuid4()), "study_id": "S1", "doc_id": "doc1",
             "parser_version": "placeholder-text-1", "embed_version": "hash-1",
             "collection": "generation1", "chunks_upserted": 1,
             "index_generation_id": "generation1", "source_revision": "revision",
             "correlation_id": "retry-trace"}

    class Insert:
        async def execute(self, statement, row):
            raise IntegrityError("INSERT", {}, RuntimeError("duplicate primary key"))

    class Read:
        async def execute(self, statement, parameters):
            # pyodbc/SQL Server can return UNIQUEIDENTIFIER as uppercase text.
            stored = {**event, "event_id": event["event_id"].upper()}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: stored))

    class Database:
        @asynccontextmanager
        async def begin(self):
            yield Insert()

        @asynccontextmanager
        async def connect(self):
            yield Read()

    assert await SqlAuditSink(Database()).record_index(event) == event["event_id"]


async def test_sql_draft_readback_keeps_the_identifier_returned_by_stream_completion():
    from contextlib import asynccontextmanager

    from medw_core.drafts import SqlDraftStore

    identifier = str(uuid.uuid4())

    class Connection:
        async def execute(self, statement, parameters):
            row = {"draft_id": identifier.upper(), "event_id": identifier.upper(), "status": "accepted"}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    class Database:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

    draft = await SqlDraftStore(Database()).get("S1", "1", identifier)
    assert draft["draft_id"] == draft["event_id"] == identifier

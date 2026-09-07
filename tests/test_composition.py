# The composition root, and the claim it exists to support.
#
# Ten Protocols are only worth the indirection if something other than the
# Azure SDK satisfies them. Until the local backend existed, every port had
# exactly one implementation and "abstraction" was an unfalsifiable claim.
# These tests make it falsifiable: each local implementation is checked
# against its Protocol by signature, not by isinstance, because
# runtime_checkable only verifies attribute names.

import dataclasses
import inspect
from contextlib import AsyncExitStack

import pytest

from medw_core import ports
from medw_core.composition import Services, build
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
from medw_core.schemas import Chunk, DocType, RetrievalFilter
from medw_core.settings import Settings


def _params(fn) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def assert_conforms(impl: type, port: type, methods: list[str]) -> None:
    for name in methods:
        assert hasattr(impl, name), f"{impl.__name__} is missing {name}()"
        expected, actual = _params(getattr(port, name)), _params(getattr(impl, name))
        assert actual == expected, (
            f"{impl.__name__}.{name}{tuple(actual)} != {port.__name__}.{name}{tuple(expected)}"
        )


@pytest.mark.parametrize(
    "impl,port,methods",
    [
        (HashEmbedder, ports.Embedder, ["embed"]),
        (InMemorySparseIndex, ports.SparseIndex, ["search", "index"]),
        (ScriptedChatClient, ports.ChatClient, ["stream", "complete_json"]),
        (FixtureLayoutExtractor, ports.LayoutExtractor, ["extract"]),
        (DictionaryEntityExtractor, ports.EntityExtractor, ["extract"]),
        (InMemoryJobStore, ports.JobStore, ["create", "advance", "fail", "get"]),
        (InMemoryAuditSink, ports.AuditSink, ["record_generation", "record_index"]),
        (InMemorySessionStore, ports.SessionStore, ["get", "put"]),
        (InMemoryDocumentStore, ports.DocumentStore, ["upsert", "by_study"]),
    ],
    ids=lambda x: getattr(x, "__name__", str(x)) if isinstance(x, type) else "",
)
def test_local_implementation_conforms_to_its_port(impl, port, methods):
    assert_conforms(impl, port, methods)


def test_sparse_port_is_not_azure_shaped():
    """The port that justifies the whole exercise.

    Cognitive Search filters with OData strings. Had SparseIndex been typed
    `filter: str`, no local index could have satisfied it and the abstraction
    would have been the Azure SDK with extra steps. The proof is that a BM25
    dict implementation takes the same RetrievalFilter the OData adapter does.
    """
    assert _params(ports.SparseIndex.search) == _params(InMemorySparseIndex.search)
    assert "flt" in _params(InMemorySparseIndex.search)


async def test_local_backend_wires_every_port_without_azure():
    """MEDW_BACKEND=local must start with no credential and no network.

    If this needs `az login`, the local backend is not local and CI cannot run
    anything that touches the composition root.
    """
    async with AsyncExitStack() as stack:
        svc = await build(Settings(backend="local"), stack)
    assert svc.backend == "local"
    for field in ("embedder", "sparse", "chat", "layout", "entities",
                  "jobs", "sessions", "documents", "audit"):
        assert getattr(svc, field) is not None, f"{field} unwired under local"


async def test_services_is_frozen():
    """A service cannot swap a dependency after startup. If it could, the
    composition root would only describe what things were wired to initially."""
    async with AsyncExitStack() as stack:
        svc = await build(Settings(backend="local"), stack)
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.embedder = None            # type: ignore[misc]


async def test_require_names_the_missing_dependency():
    """Services get only what they need. Reaching for something absent should
    say which thing and where to fix it, not raise AttributeError on None."""
    async with AsyncExitStack() as stack:
        svc = await build(Settings(backend="local"), stack)
    with pytest.raises(RuntimeError, match="classifier"):
        svc.require("classifier")


def test_services_fields_are_all_optional_ports():
    """Every dependency is a Protocol, never a concrete class — otherwise a
    handler could reach past the port for `._client`."""
    hints = Services.__annotations__
    concrete = [
        name for name, ann in hints.items()
        if name != "backend" and "ports." not in str(ann)
    ]
    assert not concrete, f"non-port fields on Services: {concrete}"


# --- the local implementations actually behave ---------------------------


async def test_hash_embedder_is_deterministic_and_correctly_shaped():
    e = HashEmbedder(dimensions=64)
    a, b = await e.embed(["Grade 3 neutropenia"]), await e.embed(["Grade 3 neutropenia"])
    assert a == b, "a fake that is not deterministic is worse than no fake"
    assert len(a[0]) == 64


def test_hash_embedder_version_cannot_collide_with_a_real_index():
    """embed_version is baked into the Qdrant collection name. A local vector
    landing in a collection alongside real ones would be undetectable."""
    assert "local" in HashEmbedder().embed_version


async def test_in_memory_sparse_honours_every_filter_field():
    """Same invariant as the OData adapter: a filter field silently ignored
    here is the section_prefix bug, reintroduced on the local side."""
    idx = InMemorySparseIndex()
    chunks = [
        Chunk(id="a", study_id="S1", doc_id="d", doc_type=DocType.tfl,
              section_path="14.3.2", kind="table_rows", text="neutropenia grade 3", ordinal=0),
        Chunk(id="b", study_id="S1", doc_id="d", doc_type=DocType.protocol,
              section_path="9.5.1", kind="prose", text="neutropenia grade 3", ordinal=0),
        Chunk(id="c", study_id="S2", doc_id="d", doc_type=DocType.tfl,
              section_path="14.3.2", kind="table_rows", text="neutropenia grade 3", ordinal=0),
    ]
    await idx.index(chunks)
    q = "neutropenia"

    assert {r[0] for r in await idx.search(q, RetrievalFilter(study_id="S1"), limit=10)} == {"a", "b"}
    assert {r[0] for r in await idx.search(
        q, RetrievalFilter(study_id="S1", section_prefix="14."), limit=10)} == {"a"}
    assert {r[0] for r in await idx.search(
        q, RetrievalFilter(study_id="S1", doc_types=[DocType.protocol]), limit=10)} == {"b"}
    assert {r[0] for r in await idx.search(
        q, RetrievalFilter(study_id="S1", kind="prose"), limit=10)} == {"b"}


async def test_audit_sink_exposes_no_way_to_mutate_the_trail():
    """Mirrors db/sql/0003_grants.sql, where the principal has INSERT and
    deliberately not UPDATE or DELETE. A fake that allowed mutation would let
    a test pass for behaviour impossible against the real store."""
    sink = InMemoryAuditSink()
    await sink.record_generation({"study_id": "S1"})
    assert len(sink.generations) == 1
    assert isinstance(sink.generations, tuple)
    for verb in ("update", "delete", "remove", "clear"):
        assert not hasattr(sink, verb), f"AuditSink must not expose {verb}()"


async def test_job_store_scopes_reads_by_study():
    """The Cosmos read is partitioned by /study_id. A local store that ignored
    it would let a cross-study bug pass here and fail in the cloud."""
    store = InMemoryJobStore()
    job = await store.create("S1", "doc-1")
    assert await store.get("S1", job["id"]) is not None
    assert await store.get("S2", job["id"]) is None


async def test_scripted_chat_records_prompts_and_streams():
    """Streaming has to be exercised as streaming — a fake that returns one
    lump would let a consumer that assumes buffering pass."""
    chat = ScriptedChatClient(responses=["numeric spine intact"])
    chunks = [c async for c in chat.stream("draft section 12.2.1")]
    assert len(chunks) > 1
    assert "".join(chunks).strip() == "numeric spine intact"
    assert chat.calls == ["draft section 12.2.1"]


async def test_cosmos_repos_satisfy_the_new_ports():
    """The azure backend wires SessionRepo and DocumentRepo into those fields.

    Checked by signature here rather than at startup, because a mismatch would
    otherwise only appear the first time a handler called the store - which is
    in production, under load, on a path with no local coverage.
    """
    from medw_core.cosmos import DocumentRepo, SessionRepo

    assert_conforms(SessionRepo, ports.SessionStore, ["get", "put"])
    assert_conforms(DocumentRepo, ports.DocumentStore, ["upsert", "by_study"])


async def test_session_store_is_scoped_by_user():
    """Cosmos partitions sessions by /user_id. A local store keyed on
    session_id alone would let a cross-user read pass here and fail there."""
    store = InMemorySessionStore()
    await store.put({"user_id": "u1", "session_id": "s1", "study_id": "ABC-101"})
    assert await store.get("u1", "s1") is not None
    assert await store.get("u2", "s1") is None


async def test_document_store_upsert_is_idempotent():
    """Document IDs are deterministic, so re-ingesting the same document must
    overwrite rather than duplicate - the property the chunk IDs give Qdrant."""
    store = InMemoryDocumentStore()
    for _ in range(2):
        await store.upsert({"doc_id": "d1", "study_id": "ABC-101", "doc_type": "tfl"})
    assert len(await store.by_study("ABC-101")) == 1

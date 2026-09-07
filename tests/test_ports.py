# Conformance: do the real implementations actually satisfy their ports?
#
# `isinstance(x, SomeProtocol)` is a weak check - runtime_checkable Protocols
# only verify that the attribute names exist, never that the signatures match.
# A class with `def search(self)` taking no arguments passes isinstance against
# a port whose search takes three. So these compare parameter names directly,
# which is what actually catches drift.
#
# The drift this guards against is not hypothetical: QdrantRepo.search took
# `section_prefix` and SparseRepo.search did not, and the fused ranking was
# quietly wrong for any section-scoped query. Nothing failed. A conformance
# test would have.

import inspect

import pytest

from medw_core.ports import SparseIndex, VectorIndex
from medw_core.schemas import DocType, RetrievalFilter


def _params(fn) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def assert_conforms(impl: type, port: type, methods: list[str]) -> None:
    for name in methods:
        assert hasattr(impl, name), f"{impl.__name__} is missing {name}()"
        expected = _params(getattr(port, name))
        actual = _params(getattr(impl, name))
        assert actual == expected, (
            f"{impl.__name__}.{name}{tuple(actual)} does not match "
            f"{port.__name__}.{name}{tuple(expected)}"
        )


def test_qdrant_repo_conforms_to_vector_index():
    from app.qdrant_repo import QdrantRepo

    assert_conforms(QdrantRepo, VectorIndex, ["search", "ensure_collection"])


def test_sparse_repo_conforms_to_sparse_index():
    from app.sparse_repo import SparseRepo

    assert_conforms(SparseRepo, SparseIndex, ["search", "index"])


# --- the regression: both halves must honour every filter field ---------
#
# These translate a filter into each store's dialect and assert the field
# survives. They need no client and no network - the translators are pure,
# which is itself a boundary decision worth keeping.


@pytest.fixture
def flt() -> RetrievalFilter:
    return RetrievalFilter(
        study_id="ABC-101",
        doc_types=[DocType.tfl, DocType.sap],
        section_prefix="11.4",
        kind="table_rows",
    )


def test_sparse_translator_honours_every_filter_field(flt):
    from app.sparse_repo import SparseRepo

    odata = SparseRepo(client=None)._odata(flt)

    assert "study_id eq 'ABC-101'" in odata      # shared index: scoping is a filter
    assert "doc_type eq 'tfl'" in odata
    assert "doc_type eq 'sap'" in odata
    assert "11.4" in odata, "section_prefix dropped - this was the original bug"
    assert "kind eq 'table_rows'" in odata


def test_dense_translator_honours_every_filter_field(flt):
    from app.qdrant_repo import QdrantRepo
    from medw_core.settings import Settings

    conditions = QdrantRepo(Settings())._conditions(flt)
    keys = {c.key for c in conditions}

    # study_id is absent on purpose: one collection per study means the
    # scoping is structural, not a filter that could be forgotten.
    assert "study_id" not in keys
    assert keys == {"doc_type", "section_path", "kind"}


def test_odata_escapes_quotes():
    """Study IDs and section paths are client-supplied. A single quote must
    not be able to terminate the literal and change the filter."""
    from app.sparse_repo import SparseRepo

    odata = SparseRepo(client=None)._odata(RetrievalFilter(study_id="A'B"))
    assert "'A''B'" in odata


def test_empty_filter_produces_no_dense_conditions():
    """An unfiltered search must not send an empty Filter object - Qdrant
    treats that differently from no filter at all."""
    from app.qdrant_repo import QdrantRepo
    from medw_core.settings import Settings

    assert QdrantRepo(Settings())._conditions(RetrievalFilter(study_id="ABC-101")) == []

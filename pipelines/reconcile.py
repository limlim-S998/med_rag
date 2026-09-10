# Read back both stores against one selected immutable generation manifest.
#
# reconcile_generation is the current, executable entrypoint. It verifies
# both counts and canonical payload hashes: equal chunk-ID sets alone cannot
# detect two stores containing different text under the same IDs. It raises
# on drift and never repairs or publishes an index. No scheduler is defined
# here; any scheduled operation must call this entrypoint explicitly.
#
# Drift/reconcile_study below preserve the earlier ID-only sketch for the
# file walkthrough. That legacy path is disabled, raises NotImplementedError,
# and is not evidence of a deployed nightly reconciliation job.

from __future__ import annotations

from dataclasses import dataclass

from medw_core.indexing import verify_stores
from medw_core.schemas import RetrievalFilter


@dataclass(frozen=True)
class Drift:
    """Legacy ID-set report; not used by current manifest-based reconciliation."""

    study_id: str
    dense_only: frozenset[str]    # in Qdrant, missing from Cognitive Search
    sparse_only: frozenset[str]   # in Cognitive Search, missing from Qdrant
    total_dense: int
    total_sparse: int

    @property
    def in_sync(self) -> bool:
        return not self.dense_only and not self.sparse_only

    @property
    def summary(self) -> str:
        if self.in_sync:
            return f"{self.study_id}: {self.total_dense} chunks, in sync"
        return (
            f"{self.study_id}: DRIFT - {len(self.dense_only)} dense-only, "
            f"{len(self.sparse_only)} sparse-only "
            f"(dense={self.total_dense}, sparse={self.total_sparse})"
        )

    def interpretation(self) -> str:
        """What each direction of drift usually means.

        Worth stating because the two directions have different causes and
        different urgencies, and treating them the same wastes the signal.
        """
        if self.in_sync:
            return "no action"
        if self.dense_only and not self.sparse_only:
            return (
                "search_sink failed after qdrant_sink succeeded - the usual "
                "case, since indexing writes Qdrant first. Re-run the DAG; the "
                "upserts are idempotent so it converges."
            )
        if self.sparse_only and not self.dense_only:
            return (
                "chunks in Search that Qdrant does not have. Usually a "
                "collection dropped or rebuilt under a new embed_version "
                "without the corresponding Search delete - check whether an "
                "alias flip left the old collection orphaned."
            )
        return (
            "both directions - the two stores have diverged rather than one "
            "lagging. Suspect a parser version bump applied to one sink only."
        )


async def reconcile_study(vectors, sparse, study_id: str, *, page: int = 1000) -> Drift:
    """Disabled legacy helper: its enumeration functions raise NotImplementedError.

    Use reconcile_generation with a manifest from IndexRegistry and the two
    GenerationSink adapters. A study ID alone does not select a serving
    generation, and ID equality alone does not establish content agreement.
    """
    dense_ids = await _all_dense_ids(vectors, study_id, page)
    sparse_ids = await _all_sparse_ids(sparse, study_id, page)
    return Drift(
        study_id=study_id,
        dense_only=frozenset(dense_ids - sparse_ids),
        sparse_only=frozenset(sparse_ids - dense_ids),
        total_dense=len(dense_ids),
        total_sparse=len(sparse_ids),
    )


async def _all_dense_ids(vectors, study_id: str, page: int) -> set[str]:
    # Scroll rather than search: this wants every point, not the nearest ones,
    # and issuing a similarity search with a huge limit to enumerate a
    # collection is both slow and subtly wrong - HNSW is approximate, so a
    # search cannot promise it returned everything.
    raise NotImplementedError("use reconcile_generation with a selected manifest")


async def _all_sparse_ids(sparse, study_id: str, page: int) -> set[str]:
    # Cognitive Search paginates; the filter is the same RetrievalFilter the
    # query path uses, so a study scoped for search is scoped identically here.
    _ = RetrievalFilter(study_id=study_id)
    raise NotImplementedError("use reconcile_generation with a selected manifest")


async def reconcile_generation(generation, dense, sparse) -> None:
    """Compare complete payload identity/counts to the published immutable manifest.

    Raises on drift, so schedulers surface a failure instead of reporting a
    successful ID-only comparison for two stores containing different text.
    """
    await verify_stores(generation, dense, sparse)

# Do the two indexes still hold the same chunks?
#
# The same chunk is written to Qdrant and to Cognitive Search by two different
# sinks, over two different network paths, with two different failure modes.
# They drift. The dangerous part is HOW they drift: nothing errors. The sparse
# half quietly stops returning something the dense half still finds, RRF fuses
# a shorter list, and recall drops by an amount nobody notices because there
# was never a number to compare against.
#
# This is only possible because the chunk ID is a pure function of
# (study, doc, section path, ordinal, parser version). Both stores key on the
# same ID, so reconciliation is a set difference rather than a content diff -
# no embeddings compared, no text normalised, no heuristics.
#
# Runs nightly. Deliberately read-only: it reports, it does not repair. An
# automatic repair would hide a systematic problem behind a nightly fix, and
# the interesting question about drift is why it happened, not how to paper
# over tonight's instance.

from __future__ import annotations

from dataclasses import dataclass

from medw_core.schemas import RetrievalFilter


@dataclass(frozen=True)
class Drift:
    """What each store has that the other does not."""

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
    """Compare chunk IDs for one study across both indexes.

    Takes the ports rather than concrete clients, so this runs against the
    in-memory implementations in a test exactly as it does against Qdrant and
    Cognitive Search in the nightly job.
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
    ...


async def _all_sparse_ids(sparse, study_id: str, page: int) -> set[str]:
    # Cognitive Search paginates; the filter is the same RetrievalFilter the
    # query path uses, so a study scoped for search is scoped identically here.
    _ = RetrievalFilter(study_id=study_id)
    ...

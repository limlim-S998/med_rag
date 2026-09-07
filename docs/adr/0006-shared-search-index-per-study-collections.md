# ADR 0006: one Cognitive Search index for all studies, one Qdrant collection per study.

**Status:** accepted

**Decision.** The two halves of hybrid retrieval are partitioned differently,
on purpose. Qdrant: a collection per study. Cognitive Search: a single
`csr-chunks` index filtered by `study_id`.

**Why they differ.** The cost models differ.

- Qdrant collections are free to create and a collection drop is an instant,
  complete teardown. Re-indexing one study cannot touch another, and the
  embedding version lives in the collection name so two vector spaces can
  never be compared by accident.
- Cognitive Search bills per service and per replica, not per index. Per-study
  indexes would multiply cost with no isolation benefit, since the filter is
  enforced server-side either way.

**The cost we accepted.** Client teardown is asymmetric: one Qdrant command
versus a filtered batch delete in Search. Teardown is rare; the bill is
monthly.

**The drift risk.** Two stores holding the same chunks can diverge. Mitigated
by the deterministic chunk ID: reconciliation is a set difference on IDs per
study, run nightly. Drift shows up as a quiet recall drop, never as an error,
so it has to be checked rather than waited for.

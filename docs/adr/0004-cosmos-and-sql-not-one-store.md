# ADR 0004: Cosmos and Azure SQL are not a duplicate. Two shapes of data.

**Status:** accepted

**Decision.** Cosmos holds semi-structured, high-churn state whose every read
is scoped to one partition — document metadata, ingestion job state, writer
sessions. Azure SQL holds the registry and the append-only audit trail, which
want foreign keys, constraints, and joins nobody can predict in advance.

**Why not one store.** Either direction is worse:

- *All Cosmos.* The audit trail is the one table a regulator might read.
  Referential integrity would move into application code, and "every section
  drafted from Table 14.3.2.1 across all studies" becomes a cross-partition
  scan. Enforcing integrity by convention on the evidential table is the
  wrong place to be clever.
- *All SQL.* Job state and sessions churn, their shape differs per document
  type, and their access pattern is a point read by study. That is a
  schema migration every time a document type gains a field, in exchange for
  joins nobody wants.

**The seam.** A generation produces a row in both: the fat payload (prose,
slots, judgement JSON) goes to Cosmos with a 90-day TTL, the joinable
permanent row goes to `audit.generation_event`. Correlation ID links them.

**Cost.** Two stores, two auth models, two backup stories. Accepted because
the audit table's requirements are genuinely different in kind, not in degree.

# db — which store holds what

Five things persist. The interesting question is never "which database is
best", it is "which of these five shapes of data is this".

| Data | Store | Why that one |
|---|---|---|
| Source documents (bytes) | **Blob Storage** | Immutable blobs, cheap, lifecycle-managed. Source of record for a reprocess. |
| Parsed layout + `ParsedTable` JSON | **Blob Storage** (`parsed/`, keyed by parser version) | Re-deriving it costs a per-page Document Intelligence call. Cache it, versioned. |
| Chunk vectors | **Qdrant** | Payload-filtered ANN, one collection per study, embedding version in the collection name. |
| Chunk text (lexical) | **Azure Cognitive Search** | BM25 over the same chunks. Literal matches embeddings destroy: `Table 14.3.2.1`, `Grade 3`, MedDRA PTs. |
| Document metadata, job state, sessions | **Cosmos DB** | Shape churns, write rate is high, every read is scoped to one partition. No joins wanted. |
| Study/document registry, audit trail | **Azure SQL** | Foreign keys, constraints, and ad-hoc joins a regulator might ask for. Append-only by grant. |

The chunk exists in three places at once — vector in Qdrant, text in Cognitive
Search, and its structured `ParsedTable` spine in the Qdrant payload. That is
deliberate duplication, and the deterministic chunk ID (`medw_core.ids`) is
what keeps the three in step: all of them are idempotent upserts keyed by the
same hash, so a re-run converges instead of drifting.

## Cosmos vs SQL — the duplicate question

They are not the same store used twice, and this is the one asked about most
(see [ADR 0004](../docs/adr/0004-cosmos-and-sql-not-one-store.md)).

- Cosmos answers *"what is the state of this thing right now"*, at high write
  rate, for documents whose fields differ by document type. Partition key
  `/study_id` means every one of those reads is single-partition.
- SQL answers *"across everything, show me…"* — every section drafted from
  Table 14.3.2.1, every generation on a superseded prompt bundle, every
  numeric-fidelity failure last quarter. Those are joins over an append-only
  log with integrity constraints, and that is a relational question.

Putting the audit trail in Cosmos would have meant enforcing referential
integrity in application code. For the one table a regulator might read, that
is the wrong place to enforce it.

## Layout

```
db/
├── sql/
│   ├── 0001_core.sql       studies, documents, sections — the registry
│   ├── 0002_audit.sql      append-only generation + verification events
│   └── 0003_grants.sql     the app principal can INSERT on audit. Not UPDATE.
└── cosmos/
    └── containers.json     containers, partition keys, TTL, indexing policy
```

Migrations run from the pipeline as a separate stage before the image rolls
(`make migrate`). Schema changes are additive-only in prod — the audit table
is never altered in place, because "we changed the audit table" is a sentence
you do not want to say in an inspection.

# Architecture — every service, every store, and what touches what

Five application services, six backing stores, two entry points into
ingestion. Nothing here is exotic; the interesting parts are the boundaries.

**Read this with two things in mind.**

*The diagram shows the target system.* Several of the request-path
implementations are held back on the `implementation/retrieval-slice` branch
while the platform is reviewed separately — the shells, contracts and wiring
are all here, the domain logic is not. See the README section *Why finished
files are missing*.

*Nothing talks to a concrete dependency directly.* Every arrow crossing into
"Azure services" goes through a Protocol in `medw_core.ports`, resolved once at
startup by `medw_core.composition`. The same diagram describes
`MEDW_BACKEND=local`, where the Azure boxes are replaced by in-memory
stand-ins and the arrows are unchanged. That substitutability is the point of
the port layer, and it is checked by tests rather than asserted.

## The whole picture

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'background':'#ffffff','primaryColor':'#eef2f8','primaryTextColor':'#111827',
  'primaryBorderColor':'#64748b','secondaryColor':'#e2e8f0','tertiaryColor':'#f1f5f9',
  'lineColor':'#475569','textColor':'#111827','fontSize':'14px',
  'clusterBkg':'#f8fafc','clusterBorder':'#94a3b8','edgeLabelBackground':'#ffffff'
}}}%%
%% Colours are pinned rather than inherited. Mermaid's auto theme follows the
%% VIEWER's light/dark setting, which renders this diagram dark-on-dark in a
%% dark VS Code preview. Pinning means one appearance everywhere - VS Code,
%% GitHub, and docs/architecture.html - which is what you want for a diagram
%% you might put on a screen in front of someone.
flowchart TB
    W([Medical writer]) -->|Entra ID token| GW

    subgraph AKS["AKS · namespace medw"]
        GW[gateway<br/><i>auth · session · correlation ID</i>]
        RT[retrieval<br/><i>dense ∥ sparse → RRF → rerank</i>]
        RR[reranker<br/><i>cross-encoder · the only weights in-cluster</i>]
        GEN[generation<br/><i>numeric spine · stream · verify</i>]
        IW[ingestion-worker<br/><i>single doc, synchronous</i>]
        QD[(Qdrant<br/>StatefulSet)]
    end

    subgraph AZ["Azure services"]
        AOAI[[Azure OpenAI<br/>gpt-4.1-mini · text-embedding-3-large]]
        ACS[(Cognitive Search<br/>BM25)]
        BLOB[(Blob Storage<br/>raw · parsed · snapshots)]
        COS[(Cosmos DB<br/>documents · jobs · sessions)]
        SQL[(Azure SQL<br/>registry · audit)]
        DI[[Document Intelligence]]
        LANG[[Azure AI Language<br/>clinical NER · UMLS]]
        AML[[Azure ML<br/>tracking · registry]]
        AI[[App Insights]]
    end

    AF[Airflow DAG<br/><i>bulk</i>]

    GW --> RT & GEN & IW
    GW --> COS
    GW -->|user-delegation SAS| BLOB
    RT --> QD & ACS & AOAI
    RT --> RR
    GEN --> AOAI & LANG & SQL & COS
    IW --> DI & LANG & AOAI & BLOB & QD & ACS & COS & SQL
    IW -->|pinned version| AML
    AF --> DI & AOAI & QD & ACS & BLOB
    GW & RT & RR & GEN & IW -.->|correlation ID| AI
```

## The port layer

Between every service and every external dependency sits a Protocol.

| Port | Azure | Local |
|---|---|---|
| `Embedder` | Azure OpenAI `text-embedding-3-large` | seeded hash embedder |
| `ChatClient` | Azure OpenAI `gpt-4.1-mini` | scripted responder |
| `VectorIndex` | Qdrant | Qdrant — real in both |
| `SparseIndex` | Cognitive Search (OData filters) | BM25 over a dict |
| `LayoutExtractor` | Document Intelligence | recorded layout JSON |
| `EntityExtractor` | Azure AI Language | dictionary matcher |
| `SessionStore` / `DocumentStore` / `JobStore` | Cosmos | in-memory |
| `AuditSink` | Azure SQL, INSERT-only by grant | append-only list |
| `Reranker` / `TableClassifier` | cross-encoder / sklearn | — |

`SparseIndex` is the one that justifies the layer. Cognitive Search filters
with OData strings; a local BM25 index has no such concept. Had the Protocol
been typed `filter: str`, no second implementation could ever have satisfied
it — the abstraction would have been the Azure SDK with extra steps. It takes
a structured `RetrievalFilter` instead, and both halves of hybrid retrieval
translate the same object into their own dialect.

## Who talks to what, and why

| Service | Reads | Writes | Notes |
|---|---|---|---|
| **gateway** | Cosmos `sessions`, SQL `core` | Cosmos `sessions`, SQL `section_draft` | The only public address. Mints the correlation ID. |
| **retrieval** | Qdrant, Cognitive Search, AOAI embeddings | — | No write credential on any store, by design. |
| **reranker** | — | — | Calls no Azure service at all. Its identity holds zero roles. |
| **generation** | AOAI, Azure Language, SQL `core` | SQL `audit`, Cosmos `generations` | The only service with ODBC in its image. |
| **ingestion-worker** | Blob, Document Intelligence, Language, AOAI, Azure ML | Blob, Qdrant, Cognitive Search, Cosmos, SQL `core` + `audit.index_event` | The widest identity in the system. |
| **Airflow DAG** | same | same | Same functions as the worker, expanded across tasks. |

## The two paths through ingestion

Bulk (Airflow) and single-document (the worker) share every function in
`pipelines/`. The DAG expands them across tasks so Airflow can retry and
parallelise; the worker awaits them in order because one writer is waiting on
one document. Separate implementations would drift, and then a document would
be chunked differently depending on how it happened to arrive.

Both are idempotent, because chunk IDs are a pure function of
`(study, doc, section_path, ordinal, parser_version)`. That is what makes a
blind re-run after a parser fix safe, and it is the property the whole
backfill story rests on.

## The request path, end to end

1. Gateway validates the token, authorises the study, mints a correlation ID.
2. Retrieval embeds the query (same deployment as index time — a different one
   silently returns garbage), fires Qdrant and Cognitive Search concurrently.
3. RRF fuses the two rank lists. Top ~30 to the cross-encoder, top 5–8 out.
4. Generation renders the deterministic numeric spine from the `ParsedTable`
   in the payload, asks the model for connective prose only, streams it back.
5. Verification: numerals diffed against slots, E3 structural rules checked,
   the judgement call to the LLM with a Pydantic-validated schema.
6. One row into `audit.generation_event` with every version identifier, the
   source chunk IDs, and each layer's verdict.

## Cross-cutting concerns

Three things every service does, each defined once:

- **Correlation.** One writer action = one ID, minted at the gateway,
  forwarded on every hop, landing in the audit row. `medw_core.tracing`.
- **Provenance.** A frozen stamp of the five version axes, captured at
  startup rather than read at write time — during a rollout those differ, and
  a row reporting intended configuration is confidently wrong exactly then.
  `medw_core.provenance`.
- **Metrics.** One set of OpenTelemetry instruments, two readers: Application
  Insights for the analytical signals (cost, recall, drift) and Prometheus for
  the one operational signal KEDA scales on. `medw_core.metrics`.

## Delivery

```
commit ──► Azure Pipelines: build, push image:<git-sha>
                            │
                            └──► commit the tag into a values file
                                          │
                                   Flux reconciles ──► cluster
```

The pipeline never deploys. Its last act is a commit, and Flux applies it.
Rollback is `git revert` — proven on a local cluster, gateway rolled forward by
one commit and back by another with nobody running `helm`.

Consequence worth knowing: once Flux owns a release, a manual `helm upgrade` is
**rejected** with a field-manager conflict. The cluster is git-owned. To change
it, change git.

## What is deliberately not here

- **Frontend.** Owned by a different pod. The clickable-citation UI is theirs.
- **Bicep / subscription-level IaC.** A platform function. `infra/bootstrap.sh`
  is the shape of the account, not the way it was provisioned in anger.
- **The FDA rule catalogue.** Defined by the client's regulatory lead. This
  repo has the layer that executes rules and the harness that tests it.
- **Prompt content.** Written by the medical writers with a client SME. What
  is here is the versioning, serving and evaluation harness around it.
- **The domain implementations**, temporarily — parsing, chunking, RRF,
  slot extraction, verification, the classifiers. On the
  `implementation/retrieval-slice` branch, to be reintroduced one at a time.
  Their absence is why the repo currently proves *structure* thoroughly and
  *behaviour* barely at all.

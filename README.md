# medwriter-assist

A working-directory skeleton for the Medical Writing Assistant. Not a
finished product — a repo shaped the way that system would actually be
shaped, so the architecture you can describe in an interview has a physical
layout attached to it.

Files split into two kinds:

- **Real code.** ~20 files with actual implementations and comments that say
  *why*, not *what*. These are the ones to read.
- **Stubs.** Files that exist to make the tree honest — the route handlers,
  the generation chains, the CLI. `...` where a body would go. They still
  carry the reasoning in comments; the shape is the point.

Every service in the stack appears somewhere, including the ones that are one
file. If a store is named in the prep doc, it has a schema, a client and a
place in the tree here — because "we used Cosmos" is a claim, and a partition
key is not.

---

## The tree

```
medwriter-assist/
├── libs/medw_core/            shared library, installed into every service
│   ├── ports.py               ★★ the seams. 10 Protocols = the architecture.
│   ├── errors.py              ★ shared error model; retryability is a property
│   ├── settings.py            ★ config → env var → Helm value. One place.
│   ├── azure.py               ★ DefaultAzureCredential + every SDK client
│   ├── ids.py                 ★ deterministic chunk IDs; collection naming
│   ├── schemas.py             ★ domain types incl. RetrievalFilter
│   ├── projections.py         ★★ Chunk → Qdrant payload / Search doc. One place.
│   ├── cosmos.py              ★ containers + the partition-key decision
│   ├── sql.py                 ★ AAD token → ODBC; the audit INSERT
│   ├── blob.py                three containers, three lifecycles
│   ├── language.py            clinical NER + UMLS (and what it is NOT)
│   ├── metrics.py             ★ the ML signals, not just latency
│   ├── auth.py                Entra ID validation — gateway only
│   ├── rate_limit.py          token bucket + Retry-After backoff for TPM quota
│   └── tracing.py             correlation ID: one writer action = one trace
│
├── pipelines/                 the batch side
│   ├── parsers/table.py       ★ Document Intelligence cells → header stack
│   ├── parsers/chunker.py     ★ row-group chunking; small-to-big prose
│   ├── parsers/doc_intelligence.py  ★ prebuilt-layout, and why not -document
│   ├── parsers/clinical_ner.py      Azure Language at ingest time
│   ├── parsers/readers.py     LlamaIndex — and where the LangChain line falls
│   ├── sinks/qdrant_sink.py   ★ the write path; alias flip on embed change
│   ├── sinks/search_sink.py   ★ the other write path; keeping two stores in step
│   ├── dags/ingest_study.py   Airflow DAG (bulk); worker shares the functions
│   ├── dags/backfill_reindex.py  ★ parser bump vs embed bump; the eval gate
│   └── cli.py                 stub — the seam DAG and worker both call
│
├── services/
│   ├── gateway/               ★ auth, session, SAS upload, correlation IDs
│   │   ├── app/main.py        the only service with a public address
│   │   └── app/routes/        search, draft (streaming), documents
│   ├── retrieval/
│   │   ├── app/main.py        ★ async FastAPI, probes, dense∥sparse, rerank
│   │   ├── app/fusion.py      stub — RRF held back (see ROADMAP)
│   │   ├── app/qdrant_repo.py ★ collections, payload indexes, pre-ANN filters
│   │   ├── app/sparse_repo.py Cognitive Search BM25 half
│   ├── reranker/app/main.py   ★ service shell + probe semantics; model held back
│   ├── generation/
│   │   ├── app/table_to_text.py ★ deterministic numeric spine
│   │   ├── app/verify.py        ★ four verification layers
│   │   ├── app/audit.py         ★ effective config, not intended config
│   │   ├── app/main.py          ★ streaming, per-pod TPM bucket, audit write
│   │   └── app/prompts/         the harness, not the clinical content
│   └── ingestion_worker/      ★ synchronous single-doc path
│       └── app/jobs.py        ★ ingestion as an explicit state machine
│
├── db/                        ★★ which store holds what, and why
│   ├── sql/0001_core.sql      studies, documents, E3 shell, drafts
│   ├── sql/0002_audit.sql     ★ append-only generation + index events
│   ├── sql/0003_grants.sql    ★ append-only by grant, not by promise
│   └── cosmos/containers.json ★ partition keys, TTLs, indexing policy
│
├── ml/                        the models you actually trained
│   ├── table_classifier/      ★ TF-IDF + LinearSVC; the CT target
│   ├── reranker_baseline/     LightGBM ranker the cross-encoder had to beat
│   ├── registry.py            ★ pinned version, never `latest`
│   └── azureml/               the command job CT triggers
│
├── evals/
│   ├── run_retrieval_eval.py  ★ recall@k, MRR against the golden set
│   └── golden_set.jsonl       3 rows, each illustrating a different failure
│
├── deploy/
│   ├── charts/medw-lib/       ★ library chart — deployment, service, SA
│   ├── charts/{gateway,retrieval,generation,reranker,ingestion-worker}/
│   │                          thin charts + values.yaml = the model version axis
│   ├── charts/qdrant/         ★ StatefulSet — the one workload that is stateful
│   ├── flux/{dev,staging,prod}/  what Flux reconciles each cluster to
│   ├── azure-pipelines/       ★ builds, then commits a tag. Does NOT deploy.
│   └── container-apps/        the demo host that scales to zero
│
├── infra/
│   ├── bootstrap.sh           ★★ what "using Azure" actually looks like
│   ├── teardown.sh            deleting must be easier than forgetting
│   └── search/csr-chunks-index.json  ★ analyzers, scoring profile, BM25 params
│
├── tests/                     ★ the boundaries, as tests
│   ├── test_architecture.py   services never import each other (ast walk)
│   ├── test_ports.py          ★ conformance + the section_prefix regression
│   ├── test_projections.py    ★ the two stores cannot diverge silently
│   └── test_rate_limit.py     the backoff bug that only appeared under 429s
│
├── .importlinter              ★★ the architecture, enforced. `make arch`.
├── .github/workflows/ci.yml   lint → architecture → types → tests → helm
│
├── scripts/bump_image_tag.py  ★ the pipeline's last act, as testable code
│
├── docs/
│   ├── architecture.md        ★ every service, every store, what touches what
│   ├── versioning.md          ★★ the four version axes and what enforces each
│   └── adr/                   eight ADRs = your hardest decisions
├── ROADMAP.md                 ★ where this is going, and local-vs-Azure fidelity
├── docker-compose.yml         local: real Qdrant + emulators, az-login auth
└── Makefile
```

★ = has real code worth reading. ★★ = read this first.

---

## Reading order

1. **`docs/architecture.md`** — the map. One diagram, one table of who talks to
   what. Read it before anything else so the rest has somewhere to attach.
2. **`infra/bootstrap.sh`** — the Azure account from nothing, in `az` commands.
   Every portal blade is a GUI over one of these. Pay attention to the
   workload-identity block at the bottom: that is the answer to "how does a
   pod call Azure OpenAI with no secret in the cluster".
3. **`libs/medw_core/azure.py`** — the client side of the same story.
   `DefaultAzureCredential` is the whole trick. Note the commented example at
   the bottom: `model=` takes a *deployment name*, not a model name.
4. **`libs/medw_core/settings.py` → `deploy/charts/retrieval/values.yaml`** —
   read these back to back. Same names on both sides. That's the three-axis
   versioning story as a physical fact rather than a claim.
5. **`db/README.md`** — five shapes of data, six stores, and the two questions
   ("what is the state of this thing" vs "across everything, show me…") that
   decide which is which. Then `db/sql/0002_audit.sql` and
   `db/cosmos/containers.json` for what that looks like in a schema.
6. **`services/retrieval/app/main.py`** — the request path end to end.
   `fusion.py` and `qdrant_repo.py` are the two files it leans on.
7. **`pipelines/parsers/chunker.py`** — the part that makes it clinical rather
   than generic RAG. `doc_intelligence.py` is what feeds it.
8. **`services/generation/app/table_to_text.py` + `verify.py` + `audit.py`** —
   why the little `LinearSVC` in `ml/` is load-bearing and not decoration,
   and what a defensible provenance record looks like.
9. **`deploy/azure-pipelines/retrieval.yml`** — the pipeline's last act is a
   commit, not a deploy.

---

## Where each backing service lives in the tree

Every store named in the prep doc, and the file that makes it real.

| Service | Client | Schema / config | Deployed by |
|---|---|---|---|
| Azure OpenAI | `libs/medw_core/azure.py` | `settings.py` deployment names | `bootstrap.sh` |
| Qdrant | `services/retrieval/app/qdrant_repo.py`, `pipelines/sinks/qdrant_sink.py` | `deploy/charts/qdrant/values.yaml` | Helm StatefulSet |
| Cognitive Search | `services/retrieval/app/sparse_repo.py`, `pipelines/sinks/search_sink.py` | `infra/search/csr-chunks-index.json` | `make search-index` |
| Blob Storage | `libs/medw_core/blob.py` | three containers, lifecycle policy | `bootstrap.sh` |
| Cosmos DB | `libs/medw_core/cosmos.py` | `db/cosmos/containers.json` | `bootstrap.sh` |
| Azure SQL | `libs/medw_core/sql.py` | `db/sql/*.sql` | `make migrate` |
| Document Intelligence | `pipelines/parsers/doc_intelligence.py` | `settings.docintel_model` | `bootstrap.sh` |
| Azure AI Language | `libs/medw_core/language.py` | — | `bootstrap.sh` |
| Azure ML | `ml/registry.py` | `ml/azureml/*.yml` | `bootstrap.sh` |
| App Insights | `libs/medw_core/metrics.py`, `tracing.py` | connection string in Helm values | `bootstrap.sh` |
| Hugging Face (reranker) | `services/reranker/app/main.py` | weights baked into the image | Helm |
| Container Apps (demo) | — | `deploy/container-apps/demo.yaml` | `bootstrap.sh` |

---

## The Azure mental model, in four sentences

**Hierarchy.** Subscription → resource group → resource. A resource group is
a folder with a lifecycle; `az group delete` takes everything with it, which
is why environments get their own.

**Identity.** You almost never hold a key. You hold a *credential object*,
every SDK client takes one, and `DefaultAzureCredential` resolves it
differently in AKS (workload identity), on your laptop (`az login` cache) and
in CI (federated service connection) — with identical code in all three.

**Deployments vs models.** In Azure OpenAI you provision a resource, create a
*named deployment* of a model inside it, and call the deployment name. Pin the
version in the name or Azure rolls it forward under you and your outputs
change with no commit anywhere.

**Managed ≠ magic.** Cognitive Search, Document Intelligence and Cosmos are
just services with endpoints, RBAC roles and quotas. The learning curve is
almost entirely "which role grants which action", and `az role assignment
create --assignee X --role Y --scope Z` is 90% of it — with the one exception
that Cosmos data-plane access uses a separate role family and its own command.

---

## Running it locally

```bash
cp .env.example .env
az login                          # DefaultAzureCredential picks this up
make up                           # qdrant + reranker + retrieval
make up-full                      # + gateway, generation, ingestion, emulators
make eval                         # recall@k, once you have indexed something
```

Qdrant and the reranker run for real. Blob, Cosmos and SQL run against
emulators under the `full` profile — Azurite, the Cosmos emulator, and Azure
SQL Edge. What genuinely cannot run locally is the AI layer: Azure OpenAI,
Document Intelligence, Azure Language and Cognitive Search have no emulators,
so you either point at a dev resource or run the fakes.

That dividing line is worth knowing rather than discovering: the stores have
local equivalents, the models do not.

---

## Deliberate gaps

No frontend, no Bicep, no FDA rule catalogue, no clinical prompt content.
Those sit outside the lane the prep doc defines and are named in
`docs/architecture.md`. The gaps are the same gaps you'd name in the
interview, which is the point.

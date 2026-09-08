# medwriter-assist

A reconstruction of a Medical Writing Assistant: a co-pilot that drafts
Clinical Study Report sections from protocols and statistical tables, and
verifies every number against its source.

**This repo is deliberately a platform, not a product.** The system design,
the delivery pipeline and the operational contracts are the subject. The
domain implementations — parsing, chunking, ranking, generation — have been
pulled out and kept separately so the two can be reviewed independently. See
[Why finished files are missing](#why-finished-files-are-missing).

---

## Start here (2 minutes)

```bash
make dev                    # .venv + everything importable
make check                  # lint, architecture contracts, types, tests, charts
MEDW_BACKEND=local make test
```

`make check` should be green: **147 tests, 6 architecture contracts, 0 type
errors, 7 charts and 5 Flux overlays rendering.**

Then read, in this order:

1. **[docs/architecture.md](docs/architecture.md)** — the map. One diagram,
   one table of who talks to what.
2. **[libs/medw_core/ports.py](libs/medw_core/ports.py)** — 12 Protocols. This
   is the architecture; everything else is a detail.
3. **[libs/medw_core/composition.py](libs/medw_core/composition.py)** — the
   one place implementations are chosen. `MEDW_BACKEND=local|azure`.
4. **[docs/versioning.md](docs/versioning.md)** — four version axes and what
   enforces each.
5. **[ROADMAP.md](ROADMAP.md)** — what was built, in what order, and the
   defects each phase surfaced.

---

## Why finished files are missing

Some files here are stubs (`...` bodies) whose *comments* are the content,
while the working implementations live elsewhere. That is deliberate and
recent.

The repo was built architecture-first: define the boundaries, then fill them.
Partway through, the implementation work had run ahead of the system design —
so the domain code was pulled out to keep the two reviewable separately.

**The durable copy is the `implementation/retrieval-slice` branch**
(commit `089dd50`). There is also a gitignored `holding/` directory used as a
local working copy; it is a convenience, not a backup, and will not survive a
clean checkout.

Held back:

| Area | Files |
|---|---|
| Parsing | `pipelines/parsers/table.py`, `chunker.py`, `doc_intelligence.py` |
| Pipeline seam | `pipelines/cli.py` |
| Ranking | `services/retrieval/app/fusion.py` (RRF) |
| Generation | `table_to_text.py`, `verify.py` |
| Models | `ml/table_classifier/*`, `ml/reranker_baseline/*`, the reranker's cross-encoder |
| Evaluation | `evals/run_retrieval_eval.py`, the populated golden set, DI-layout fixtures |

Kept, because these **are** the system design: all of `libs/medw_core`, every
service shell with its lifespan and probe semantics, the retrieval adapters
that prove the ports are satisfiable, and everything under `deploy/`, `infra/`,
`db/`, `tests/` and CI.

Verified: with every one of those files stubbed, the suite still passes. Nothing
in the scaffolding depends on any of them.

---

## Very brief history

Roughly in order, each phase ending in something demonstrable:

- **Contracts and boundaries.** 12 Protocols, a shared error model, and
  `import-linter` contracts that fail CI on a boundary violation.
- **One source of truth for data contracts.** `projections.py` — the Qdrant
  payload and the Search document derive from one place, checked against the
  real index definition.
- **The delivery loop.** Charts render and lint; five images build, start and
  serve correct probes; versioning enforced by tests.
- **Real Azure.** Azure OpenAI provisioned, embeddings and chat verified live
  through `DefaultAzureCredential`. Storage, AI Search, Cosmos, Document
  Intelligence and Language on free tiers.
- **Local backend + composition root.** Every port gained a second
  implementation, so `MEDW_BACKEND=local` runs the whole stack with no
  credential and no cost.
- **Cluster proof.** Full stack deployed to minikube via Flux; a commit rolled
  the gateway forward and `git revert` rolled it back, with nobody running
  `helm`.
- **Observability and autoscaling.** Prometheus scraping all four services,
  KEDA resolving the in-flight metric and scaling on it.
- **Consistency model.** Ingestion FSM with illegal transitions unrepresentable,
  per-stage retry economics, two-store drift detection.

**Eleven silent defects were found along the way**, and they are the most
useful thing in the repo — none would have surfaced from reading the code. Full
list in [ROADMAP.md](ROADMAP.md). Three of them are the same lesson: *a version
that does not move when the content moves makes caching indistinguishable from
correctness* — which bit at the image tag, the chart dependency, and the chart
version.

---

## Layout

```
libs/medw_core/            the shared library, installed into every service
  ports.py                 ★★ 12 Protocols = the architecture
  composition.py           ★★ the only place implementations are chosen
  adapters.py              Azure-side Embedder and ChatClient
  local/                   ★ a second implementation of every port
  projections.py           ★ Chunk -> Qdrant payload / Search doc. One place.
  schemas.py               domain types incl. RetrievalFilter
  provenance.py            ★ the version stamp on every audit row
  jobs.py                  ★ ingestion FSM + per-stage retry economics
  metrics.py               ★ in-flight gauge (KEDA) + analytical signals
  errors.py                shared error model; retryability is a property
  settings.py              config -> env var -> Helm value
  azure.py, cosmos.py, sql.py, blob.py, language.py, auth.py
  ids.py, tracing.py, rate_limit.py

services/                  gateway, retrieval, generation, reranker,
                           ingestion_worker — shells + probe semantics
pipelines/                 parsers, sinks, Airflow DAGs (mostly held back)
ml/                        classifiers and the Azure ML job spec
db/                        which store holds what, and why
deploy/
  charts/medw-lib/         ★ library chart: deployment, service, SA, HPA,
                             ScaledObject, Ingress, PDB, ServiceMonitor
  charts/<service>/        thin charts; values.yaml = the model version axis
  flux/{base,local,dev,staging,prod}/
  azure-pipelines/         builds, then commits a tag. Does NOT deploy.
infra/                     bootstrap.sh, teardown.sh, search index definition
tests/                     ★ 9 modules; the boundaries as executable rules
scripts/                   bump_image_tag.py, local_deploy.sh
docs/                      architecture.md, versioning.md, 8 ADRs
```

★ = worth reading. ★★ = read first.

---

## The two backends

```bash
MEDW_BACKEND=local     # in-memory everything. No credential, no network, no cost.
MEDW_BACKEND=azure     # the real services.
```

Read in exactly one place ([`composition.py`](libs/medw_core/composition.py)).
Application code never constructs a dependency — it receives one through a
port. Qdrant is the real thing in both, because it runs in a container.

The local backend exists to prove the ports are abstractions rather than the
Azure SDK renamed. That is not rhetorical: `ChatClient.stream` was an
**unsatisfiable** Protocol until a second implementation existed to check it
against.

---

## Running it

```bash
make up            # qdrant + reranker + retrieval
make up-full       # + gateway, generation, ingestion, store emulators
make check         # everything CI runs
make charts        # render + lint every chart, build every Flux overlay
make release DRY=1 # preview the image-tag bump the pipeline commits
```

Local Kubernetes (minikube; `k3d` is not in the Arch repos):

```bash
minikube start -p medw
./scripts/local_deploy.sh              # builds with an immutable per-build tag
```

Flux: see [deploy/flux/local/README.md](deploy/flux/local/README.md).

---

## Known gaps

- **Nothing is measured.** No trained classifier, no retrieval number, no CT.
  All of it blocked on the held-back implementations, not on missing design.
- **Scale-*up* is undemonstrated.** KEDA reads the metric and scales *down* on
  it, but stub handlers return in microseconds, so concurrent load never
  registers. Real handlers embed and stream.
- **Azure SQL is not provisioned** — it needs an AAD admin principal decision.
  `sql_server` is empty and `engine()` fails loudly rather than building a
  connection string to nowhere.
- **The Azure backend's readiness reports 503** with reason
  `reachability checks not implemented`. Honest, and it means nothing deploys
  to AKS until those are written.

## Deliberately out of scope

No frontend, no Bicep, no FDA rule catalogue, no clinical prompt content. Those
sit outside the lane this project is about, and building them would weaken the
story rather than strengthen it.

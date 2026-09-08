# Roadmap — architecture first, implementations after

## The ordering, and why it is this way round

The obvious plan is bottom-up: make one study flow end to end, then generalise.
This roadmap deliberately does the opposite — define the boundaries, then fill
them — for two reasons.

**It matches the lane.** The brief says the defensible depth is "the platform
and the retrieval path: service shape, the FastAPI request path, packaging,
delivery". An interviewer presses on boundaries and contracts long before they
press on whether footnote markers are bound to the right cell.

**The evidence says contracts are the live gap.** `QdrantRepo.search()` accepts
`section_prefix`. `SparseRepo.search()` does not, and the retrieval service
never passes it to the sparse half — so a section-filtered query fuses filtered
dense results with *unfiltered* sparse results. That is not a typo. It is what
happens when two repositories evolve without a shared contract, and no amount
of bottom-up implementation would have surfaced it. Writing the interface does.

## Where this stands today

**Real** (~600 lines): the retrieval path — `main.py`, `fusion.py`,
`qdrant_repo.py`, `sparse_repo.py` — plus `table.py` and `chunker.py`,
`ids.py`, `schemas.py`, `settings.py`. All twelve `medw_core` modules import.
All four service apps import under their container-shaped paths.

**Shape only**: 44 `...` bodies across 24 files, concentrated in ingestion,
the gateway routes, and generation's slot extraction.

**Missing**: `data/sample/` (referenced by `make seed`), a real
`pipelines/cli.py` (one comment line, and it is the seam the DAG, the worker
and `make seed` all call), and a `golden_set.jsonl` with real chunk IDs
instead of literal `"<uuid>"`.

Nothing in this repo has ever executed against data.

---

## The governing principle: seams

Every external dependency gets a Protocol and at least two implementations —
a real one and a local one. Not to avoid Azure, but so that *iterating* is
free and *switching* is a config change rather than a rewrite.

| Seam | Real | Local |
|---|---|---|
| `Embedder` | Azure OpenAI `text-embedding-3-large` | seeded hash embedder, or local sentence-transformers |
| `SparseIndex` | Azure Cognitive Search BM25 | in-process BM25 over the same chunks |
| `VectorIndex` | Qdrant | Qdrant (no gap — it is the real thing locally) |
| `LayoutExtractor` | Document Intelligence `prebuilt-layout` | recorded layout JSON fixtures |
| `ChatClient` | Azure OpenAI `gpt-4o` | scripted deterministic responder |
| `EntityExtractor` | Azure AI Language | dictionary matcher over a MedDRA-shaped list |

**The risk of designing seams before implementing them** is real: you produce
an abstraction that does not survive its second implementation. `SparseIndex`
is the live example — Cognitive Search filters with OData strings, and a local
BM25 has no OData. A Protocol typed `filter: str` would bake Azure into the
thing that is supposed to hide it.

**Mitigation: tracer bullets, not vertical slices.** As each contract is
defined, write the cheapest possible second implementation — usually the fake —
purely to prove the shape holds. Not the feature. That keeps the work
architecture-led while staying grounded in something that runs.

---

## Implementation currently held back

The repo is deliberately in a **scaffolding-first** state: system design, ops
and delivery are the subject, and domain/modelling implementations have been
pulled out so they can be reviewed and reintroduced one at a time.

Held back (stubbed here, with signatures and reasoning intact):

`pipelines/parsers/table.py` · `chunker.py` · `pipelines/cli.py` ·
`services/retrieval/app/fusion.py` · `services/generation/app/table_to_text.py`
· `verify.py` · the reranker's cross-encoder · `ml/table_classifier/*` ·
`ml/reranker_baseline/*` · `evals/run_retrieval_eval.py` · the populated golden
set and the DI-layout fixtures.

**The durable copy is the `implementation/retrieval-slice` branch.**
`holding/` is a gitignored working copy for convenience, not the backup — it
is not committed and will not survive a clean checkout.

Kept, because these *are* the system design: all of `libs/medw_core` (ports,
errors, schemas, settings, projections, ids, tracing, metrics, rate limiting,
Azure clients), every service shell with its lifespan and probe semantics, the
retrieval adapters that prove the ports are satisfiable, and everything under
`deploy/`, `infra/`, `db/`, `tests/`, `scripts/bump_image_tag.py` and CI.

Reintroducing is a copy back plus its tests. Nothing in the scaffolding
depends on any of it — verified: 79 tests, 4 architecture contracts, all
charts and Flux overlays still pass with every one of those files stubbed.

## Phases

### A. Contracts and boundaries — DONE

Make the architecture executable instead of aspirational.

Delivered: `medw_core.ports` (10 Protocols), `medw_core.errors`,
`RetrievalFilter` shared by both retrieval halves, `.importlinter` with 4
contracts, `tests/test_architecture.py` + `tests/test_ports.py` +
`tests/test_rate_limit.py`, ruff/mypy config that respects the stub idiom,
and `.github/workflows/ci.yml`. `make check` runs the lot.

Three real defects fell out of writing it, all pre-existing and all silent:

1. **The `section_prefix` divergence** — section-scoped queries fused scoped
   dense hits with unscoped sparse hits. Fixed by the shared filter type.
2. **The reranker had no `/readyz`** — but `medw-lib`'s deployment template
   wires a readiness probe to that path, so the pod would never have become
   ready. Its single `/healthz` also returned 200 whether or not the model had
   loaded. Found by `test_every_service_exposes_both_probes`.
3. **`with_backoff` crashed on throttling** — it dereferenced
   `e.response.headers` unconditionally, so a `RateLimitError` without a
   response raised `AttributeError` from inside the handler, killing the retry
   loop exactly when quota pressure made it necessary. Found by mypy.

That is the argument for this ordering, in three concrete items: none of them
would have surfaced from implementing features bottom-up, and all three were
sitting in code that looked finished.

### B. One source of truth for data contracts — DONE

Delivered: `medw_core.projections` owns every projection of `Chunk`. Both
sinks and the retrieval service import from it; nothing assembles a store
document by hand any more. 13 tests in `tests/test_projections.py`.

What it bought, beyond removing the duplication:

- **Round-trip is lossless.** `Chunk → payload → Chunk` is an identity, which
  required adding `ordinal` to the Qdrant payload — it was simply absent
  before, so a chunk could not be reconstructed from the store at all. That is
  a prerequisite for the reconciliation job (Phase D) and for backfilling
  without re-paying Document Intelligence per page.
- **The projection is checked against the real index definition.**
  `infra/search/csr-chunks-index.json` is parsed in the test, so adding a
  field to the projection and not to the index fails locally instead of
  mid-way through a batch ingest against the live service.
- **`coded_terms` now reaches both stores.** It was only ever written to
  Cognitive Search, so the verification pass — which diffs coded terms out of
  the retrieved chunks — had nothing to diff against on the dense path.

Verified by mutation rather than assumed: adding an undefined field, renaming
a shared field in one projection only, and dropping `ordinal` each fail the
suite. A test that never fails is not evidence.

### C. The delivery loop ← promoted to next

Everything above is architecture quality. This is a different axis: whether
the thing can actually be built, versioned and deployed. A–B could be perfect
and there would still be nothing deployable, so this comes before any more
internal work.

Promoted ahead of provenance and the composition root for two reasons. CI/CD,
versioning and delivery *are* the platform lane — the one the brief says is
defensible depth. And this phase invalidates things: rendering charts and
building images surfaces problems that reshape whatever is built on top, so
finding them now is cheaper than finding them after three more layers.

**Done in this repo:**

- All six charts render and `helm lint` clean, verified locally.
- The decorative values are now real. `medw-lib` gained `_autoscaling.yaml`,
  `_ingress.yaml` and `_pdb.yaml`, plus a `startupProbe` block. Previously
  `ingress`, `autoscaling`, `startupProbe` and `podDisruptionBudget` were
  declared in values files with careful rationale and **no template read
  them** — configuration that was documentation.
- The autoscaling fork now encodes the argument the values files were already
  making: `metric: cpu` renders an HPA (only the reranker, which genuinely is
  CPU-bound), anything else renders a KEDA `ScaledObject` on in-flight
  requests. `replicas` is omitted whenever an autoscaler owns it, so Helm and
  the autoscaler cannot oscillate against each other under Flux reconciles.
- **Qdrant had no Service at all.** Every other chart's
  `qdrant_url: http://qdrant:6333` resolved to nothing. Now a headless Service
  for peer discovery plus a ClusterIP Service for clients, with the
  StatefulSet's `serviceName` pointed at the headless one.
- **The Flux layer was notional and is now real.** It listed Helm chart
  directories as kustomize `resources:` (which kustomize cannot consume) and
  patched `HelmRelease` objects that did not exist. Rebuilt as
  `base/` (a `GitRepository` + six `HelmRelease`s) with dev/staging/prod
  overlays. All three `kubectl kustomize` cleanly.
- `medw-lib` bumped 0.3.0 → 0.4.0 with all five consumers re-pinned — the
  chart-versioning discipline exercised for real.
- `.gitignore`, and a CI workflow with four jobs: code, delivery (lint,
  render, kubeconform, flux build), and a matrix image build.
- **The retrieval image builds, starts, and serves `/healthz` 200 with
  `/readyz` 503** when its dependencies are absent — the liveness/readiness
  split working as designed rather than as described.

**Second pass — versioning and images:**

- **All five images build**, and all five start and serve their probes
  correctly. Sizes: gateway 357MB, generation 519MB, ingestion-worker 1.09GB,
  reranker 2.22GB. The reranker reranks correctly end to end — the first piece
  of real ML functionality in the repo that has ever executed.
- **The release step is real code.** `scripts/bump_image_tag.py` replaces the
  `yq -i` one-liner in the pipeline. `yq` was installed nowhere in this repo,
  so the most consequential step in the delivery path could only run on a
  build agent: untestable, and with no way to preview what it would change.
  The script refuses `latest` and any non-SHA tag, edits exactly one line, and
  preserves the comments that explain every setting.
- **The version axes are tests, not claims.** `tests/test_versioning.py` (23
  tests) asserts: no chart uses a floating tag; environment overlays never pin
  their own tag; every chat deployment carries a date suffix; ingestion and
  retrieval agree on `embed_version`; every consumer's `medw-lib` pin matches
  the library's actual version; `Chart.lock` agrees with `Chart.yaml`.
  Mutation-tested — bumping medw-lib without re-pinning, unpinning `gpt-4o`,
  pinning a tag in `values-prod.yaml`, and desyncing `embed_version` each fail.
- `make release` / `make release DRY=1`, `make charts`, and `docs/versioning.md`.

**Two more real bugs, both found by starting the containers:**

1. **generation could not boot without Azure.** `lifespan` called
   `access_token_struct(cred)`, which reaches AAD for a SQL token — so the
   process died at startup with no credentials. In-cluster, a transient AAD
   blip during a rollout would crash-loop new pods instead of letting them
   start and report unready. The SQL engine is now built lazily on first use.
   Same liveness-versus-readiness argument the repo makes everywhere else,
   applied to startup.
2. **Two readiness probes were lying.** `gateway` and `ingestion-worker`
   answered `/readyz` **200** with every dependency unreachable, because a
   `...` body returns `None` and FastAPI renders that as a 200. Kubernetes
   would have routed traffic to pods that could not serve. All unimplemented
   probes now fail closed with an explicit 503, and
   `test_no_readiness_probe_is_a_bare_stub` prevents a recurrence.

**Third pass — the stack actually runs on Kubernetes.**

minikube (k3d is not in the Arch repos; minikube was already installed and is
equivalent for Helm and Flux). All six workloads deployed and healthy:
5 Deployments, 1 StatefulSet, 7 Services, 1 Ingress.

Two findings, both from running rather than rendering:

1. **`helm --wait` cannot deploy a service whose readiness fails closed.**
   The three unimplemented `/readyz` handlers returned 503 forever, so the
   install timed out — correctly. This matters beyond Helm: the `HelmRelease`
   in `deploy/flux/base` sets `remediateLastFailure: true`, so Flux would
   install, wait, fail, retry three times and roll back, in a loop, forever.
   Fixed with `medw_core.composition.readiness`: under the local backend the
   dependencies are in-process objects, so wired means available; under azure
   it still reports 503 with the reason `reachability checks not implemented`,
   because a constructed SDK client does no I/O and reporting ready on
   "I hold an object" is the lying probe again in better disguise.

2. **A reused image tag is invisible to the kubelet.** `minikube image load
   medw-gateway:dev` plus `pullPolicy: Never` left the pods running the *old*
   binary — verified by grepping the container filesystem. No error; the pod
   restarted, reported healthy and ran stale code. This is the repo's own
   `:latest` argument arriving locally. `scripts/local_deploy.sh` now builds
   with an immutable per-build tag, the same discipline production uses.

**Still needs you:** nothing. Flux itself is not yet installed on the cluster —
that is the last step of this phase (`flux bootstrap`, then a tag bump and a
revert to prove the reconcile loop).

**Done when:** the loop closes — commit → CI green → image tagged with the git
SHA → values bump committed → Flux reconciles → pods pass both probes →
`git revert` rolls it back.

### C¾. Prometheus, to make KEDA real — DONE

Wanted in the project: **Prometheus, deployed as part of the stack**, so the
KEDA autoscaling is actually driven rather than described.

This is not a preference — it closes a chain that is currently broken in four
places, and the break is silent. A `ScaledObject` whose query returns no series
does not error; it scales to `minReplicaCount` and stays there, looking like
an autoscaler that has decided nothing needs scaling.

**The chain, as it stands:**

1. `medw-lib/templates/_autoscaling.yaml` renders a KEDA `ScaledObject` with
   `serverAddress: http://prometheus-operated.monitoring:9090`.
2. **Nothing deploys Prometheus.** That address resolves to nothing.
3. **No service exposes `/metrics`** — verified, there is no such endpoint
   anywhere in `services/`.
4. `medw_core/metrics.py` exports through `configure_azure_monitor`, i.e. to
   **Application Insights only**. There is no Prometheus reader, so even with a
   scrape endpoint there would be nothing to scrape.

**And the metric the autoscaler scales on does not exist.** The rendered query
is `sum(medw_inflight_requests{app="<name>"})`, but `metrics.py` defines
tokens-per-request, cost-per-section, retrieval hit@k, reranker score, numeric
fidelity failures and JSON retries — no in-flight gauge. The values files argue
at length that in-flight requests are the right signal for an LLM-bound
service, and nothing measures it.

**Done:**

- `medw.inflight_requests` — an UpDownCounter in `metrics.py`, the metric the
  ScaledObject was already querying and nothing emitted.
- `InFlightMiddleware` — pure ASGI, not `BaseHTTPMiddleware`, because the
  latter buffers responses and generation streams tokens. Middleware that
  disabled streaming in order to count requests would be measuring the thing
  it broke. The decrement is in a `finally`, so a raising handler cannot
  strand a count and drift the gauge upward forever.
- `configure_prometheus()` adds a second reader to the **same** meter provider,
  so App Insights and `/metrics` share one set of instruments. Verified that
  OTel forwards instruments created at import time to a provider installed
  later — an assumption worth checking, since if it were false the gauge would
  silently record nothing and produce the exact empty-query failure.
- `/metrics` on all five services.
- `tests/test_metrics.py` — 7 tests, including the one that would have caught
  the original break: every chart's `autoscaling.metric` must correspond to an
  instrument that actually exists. Mutation-tested by pointing a chart at
  `concurrent_requests` and confirming it fails.

Exported name verified end to end through the real exporter:
`medw_inflight_requests{app="retrieval"}` — matching
`sum(medw_inflight_requests{app="retrieval"})` in the rendered ScaledObject.

**Cluster half, done:**

kube-prometheus-stack (operator only — no Grafana or Alertmanager, on an 8GB
node) and KEDA installed. `_servicemonitor.yaml` added to `medw-lib`.
Verified end to end:

```
Prometheus targets in medw:  gateway up, generation up,
                             ingestion-worker up, retrieval up   (4/4)
medw_inflight_requests{app="gateway"}  = 0        (idle, correct)
keda-hpa-gateway  TARGETS 0/2 (avg)  MIN 1  MAX 4
```

KEDA resolves the query to a real number rather than `<unknown>`, and scaled
the gateway from 4 replicas back to `minReplicas` once the metric read 0 —
an actual autoscaler decision, not a rendered manifest.

**Three defects found by running it, all silent:**

1. **The Service had no label and no named port.** A `ServiceMonitor` selects
   on *Service* labels and refers to a port *by name*; `_service.yaml` had
   neither. The monitor would have matched nothing and Prometheus scraped
   nothing — an empty query, not an error.
2. **The gauge read 1 on a completely idle service.** The middleware counted
   the `/metrics` scrape itself: increment, render the gauge including that
   increment, record. KEDA divides by replica count, so a permanent floor of
   one-per-pod is load indistinguishable from real work — and a metric that
   never returns to zero can never scale back to `minReplicas`. `/metrics`,
   `/healthz` and `/readyz` are now excluded.
3. **Chart versions never moved.** All the service charts stayed at `0.1.0`
   while their templates and their `medw-lib` dependency changed underneath.
   helm-controller caches a built chart by name and version, so retrieval,
   generation and ingestion-worker kept rendering *without* the ServiceMonitor
   that gateway had. Bumping them to `0.2.0` fixed it immediately. Same class
   of failure as a reused image tag: a version that does not move when the
   content moves makes caching indistinguishable from correctness.

**What is NOT demonstrated:** an actual scale-*up*. The handlers are stubs that
return in microseconds, so 40 concurrent requests produce roughly zero
concurrency at any scrape instant. Real handlers embed and stream, which take
seconds — this becomes demonstrable when the implementations come back out of
`holding/`, not before.

Locally this is also what makes the autoscaling path testable at all: KEDA and
Prometheus both install into minikube, so the trigger can be exercised without
AKS.

**Why not just use Azure Monitor — it is already wired.**

A fair challenge, and the answer is not "Azure Monitor cannot do it". KEDA has
an `azure-monitor` scaler, so scaling on an App Insights metric with no
Prometheus at all is a real option. Three reasons it is the wrong tool for
*this* trigger:

- **Freshness.** Azure Monitor metric ingestion lags by minutes. Prometheus
  scrapes in-cluster every 15-30s. A signal whose whole purpose is reacting to
  a burst of concurrent LLM calls is useless at two minutes stale - you scale
  up after the burst has passed and down before the next one.
- **Blast radius.** The KEDA operator would need its own workload identity and
  a role assignment to read Azure metrics. In-cluster Prometheus needs neither.
- **Locality.** The `azure-monitor` scaler cannot run on minikube, so the
  autoscaling path would have no local exercise at all.

**These are not either/or, and the split is the point.** Azure Monitor stays,
and keeps the *analytical* signals - cost per section, recall@k, numeric
fidelity failures, drift - because those want long retention and ad-hoc
queries. Prometheus carries only the *operational* signal the autoscaler
reads. OpenTelemetry supports multiple readers on one meter provider, so the
same instrument feeds both without a second definition:

| Signal | Destination | Why |
|---|---|---|
| in-flight requests | Prometheus | seconds-fresh, drives scaling |
| tokens, cost, recall@k, drift | Application Insights | long retention, ad-hoc analysis |

**Third option worth pricing before committing:** *Azure Monitor managed
service for Prometheus*. Azure operates the Prometheus server and it
integrates with AKS, while KEDA keeps using its `prometheus` scaler - so
`_autoscaling.yaml` barely changes and nobody runs a StatefulSet. Check
availability and cost in `australiaeast` first; this has not been verified.

**Unrelated, since it came up:** Flux has nothing to do with any of this. Flux
is GitOps - it reconciles the cluster to what is committed. Azure Monitor and
Prometheus are observability. The two concerns do not touch.

### D. Provenance as a cross-cutting concern

Three orthogonal version axes are claimed. Architecturally there should be one
object that stamps them onto audit rows, metric dimensions and log records —
today that is duplicated across `audit.py`, `metrics.py` and `settings.py`.

**Done when:** one `Provenance` value is threaded to every exit point, and
adding a fourth axis is a one-line change.

### E. Consistency and failure model — DONE

Was prose comments. Now properties.

- **`medw_core/jobs.py`** — the state machine as data. Transitions are derived
  from the linear pipeline, so the interesting content is what the map omits:
  there is no edge from `queued` to `indexing`, so a bug that skipped
  extraction cannot produce an indexed document with no parsed artefact behind
  it. `done` and `failed` are terminal — a completed job is re-run by creating
  a new one, so the record of what happened survives instead of being
  overwritten by the retry.
- **It lives in `medw_core`, not the service.** Two things implement
  `JobStore`, and if each enforced its own rules a job could take a path
  locally that Cosmos rejects — the worst kind of divergence, because it only
  appears in the environment you cannot attach a debugger to. The store
  persists; the rules are shared.
- **`RETRY_COST`** — the retry decision is not "how many times" but "is this
  stage safe and cheap to repeat", and the answers differ per stage.
  Extraction gets **one** attempt because Document Intelligence bills per page
  and a TFL package is hundreds; embedding gets **five** because 429 is its
  expected failure and backing off is the correct response. Every entry
  records whether repeating it costs money, quota or nothing.
- **`pipelines/reconcile.py`** — two-store drift as a set difference on chunk
  IDs, possible only because the IDs are a pure function of the content path.
  Read-only by design: an automatic repair would hide a systematic problem
  behind a nightly fix, and the interesting question about drift is why it
  happened. `Drift.interpretation()` distinguishes the two directions, which
  have different causes and different urgencies.
- **26 tests**, mutation-verified: allowing a stage to be skipped, and giving
  extraction as many retries as embedding, each fail the suite.

### F. Composition root

The `ctx: dict` in each `lifespan()` is ad-hoc wiring. A composition root is
what turns the seam strategy into a single switch instead of conditionals
scattered through the services.

**Done when:** `MEDW_BACKEND=local|azure` selects every implementation, in one
place, with no `if` statements in application code.

### G. Descend into implementations

Only now, and in whatever order is interesting: the sample fixtures, a real
`pipelines/cli.py`, slot extraction, structural rules, the job state machine,
the gateway routes. By this point these are filling in shapes rather than
discovering them.

The highest-value items when you get here:

1. Recorded Document Intelligence layout fixtures, so `table.py` — the code
   that makes this clinical rather than generic RAG — is exercised for real
   with no per-page cost.
2. A real `golden_set.jsonl` and `make eval`, so the reranker claim becomes a
   measurement. Report it as a *local harness* number (see below).
3. The tampered-numeral test: prove `numeric_fidelity` fails a section when a
   numeral is altered. That single test is the proof the anti-hallucination
   design works, and it needs no model at all.

### H. Real Azure, deliberately

See the fidelity analysis below. Order by learning-per-pound: the CLI and RBAC
first, then the free-tier services, then Azure OpenAI on credit, then a single
short AKS session for workload identity.

---

## Local vs real Azure

Three categories, and conflating them is where this goes wrong.

### Faithful locally

Qdrant is the real thing — no gap at all. Azurite covers ordinary blob
semantics well.

### Emulated, with gaps that matter *to this design specifically*

| Emulator | Silently missing | Why it bites |
|---|---|---|
| Azurite | AAD auth, user-delegation SAS, lifecycle policies | The gateway's upload design *is* user-delegation SAS. Untestable locally. |
| Cosmos emulator | AAD data-plane RBAC, real RU accounting | The control-plane/data-plane RBAC split is the gotcha documented in ADR 0005 — and it is exactly what the emulator omits. |
| SQL Edge | AAD auth entirely, `CREATE USER … FROM EXTERNAL PROVIDER` | `db/sql/0003_grants.sql` is the append-only guarantee. It cannot run on SQL Edge. |

The pattern is not a coincidence: **the emulator gaps line up almost exactly
with the identity story**, which is the most valuable Azure content here.

### No emulator at all

Azure OpenAI, Document Intelligence, AI Language, Cognitive Search.

Cognitive Search is the one that distorts results rather than merely blocking
them. Its BM25 scoring, analyzers and scoring profile are the *behaviour* the
design depends on. A local BM25 ranks differently, so RRF fuses differently,
so **recall numbers measured locally do not transfer to the cloud.** Report
them as local-harness numbers or they mislead — including in an interview.

### Only learnable in the cloud

- The workload identity federated token exchange.
- Which RBAC role actually grants which action.
- Real 429 and `Retry-After` behaviour — `rate_limit.py` is designed against it.
- Deployment-name-vs-model-name, for real.
- The Cosmos control-plane vs data-plane RBAC split.

### Posture

Local for the loop, cloud for the learning. A **cloud day**: set a budget
alert, provision into one resource group, exercise only what emulators cannot
reach, record the results, `az group delete`. The two things that cost real
money if forgotten are AKS node pools and a Standard-tier Search service.

**The bridge is recorded fixtures.** Run the contract tests against real Azure
once, save the responses, replay them locally. That keeps the fakes honest
rather than drifting into wishful thinking — and it is a strong answer to "how
did you test against services you could not run?"

---

## What stays out of scope

Unchanged, and still correct: no frontend, no Bicep, no FDA rule catalogue, no
clinical prompt content. Those are the deflection lines in the prep doc, and
building them would weaken the story rather than strengthen it.

---

## Historical note on the handover section

An earlier version of this file ended with step-by-step instructions to install
k3d and run `helm install` by hand. Both are now wrong:

- **k3d is not in the Arch repos** (AUR only, flagged out of date). minikube
  was already installed and is arguably the closer analogue to AKS anyway,
  since k3s is a trimmed distribution that swaps components (servicelb for a
  cloud load balancer, Traefik for nginx) while AKS is upstream Kubernetes.
- **Manual `helm upgrade` is now rejected.** Flux owns these releases, so a
  hand-run upgrade fails with a field-manager conflict against helm-controller.
  The cluster is git-owned: to change it, change git.

Current instructions live in [deploy/flux/local/README.md](deploy/flux/local/README.md)
and `scripts/local_deploy.sh`.

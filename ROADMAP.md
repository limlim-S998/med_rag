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

**Still needs you** (see the handover at the end of this file): `git init` and
a remote, the first CI run, the other four images, and a local k3d cluster to
prove the reconcile loop.

**Done when:** the loop closes — commit → CI green → image tagged with the git
SHA → values bump committed → Flux reconciles → pods pass both probes →
`git revert` rolls it back.

### D. Provenance as a cross-cutting concern

Three orthogonal version axes are claimed. Architecturally there should be one
object that stamps them onto audit rows, metric dimensions and log records —
today that is duplicated across `audit.py`, `metrics.py` and `settings.py`.

**Done when:** one `Provenance` value is threaded to every exit point, and
adding a fourth axis is a one-line change.

### E. Consistency and failure model

Currently prose comments. Make them properties.

- Property test: upserting twice equals upserting once, for both sinks.
- Two-store reconciliation as a real function (set difference on chunk IDs).
- The ingestion job FSM with illegal transitions unrepresentable.
- Explicit retry boundaries: which stage is safe to re-run, and what it costs.

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

## Handover: finishing Phase C

Four things are left, in order. Each is verifiable on its own.

### 1. Put it under git

```bash
cd medwriter-assist
git init -b main
git add -A && git commit -m "medwriter-assist: architecture, contracts and delivery scaffold"
git remote add origin git@github.com:<you>/medwriter-assist.git
git push -u origin main
```

Then create a branch to work on — this is the command to reach for:

```bash
git switch -c phase-c/delivery-loop
```

`git switch -c <name>` creates and checks out in one step (`git checkout -b`
is the older spelling of the same thing). Push it the first time with
`git push -u origin phase-c/delivery-loop`; after that plain `git push`.

Then update the placeholder in `deploy/flux/base/source.yaml` — it says
`CHANGEME`, and Flux needs the real URL.

### 2. Build the remaining images

`retrieval` is verified. The rest use the same pattern:

```bash
docker build -f services/gateway/Dockerfile -t medw-gateway:dev .
```

Same for `generation` and `ingestion_worker`. Note the build context is `.`
(the repo root), not the service directory — the Dockerfiles copy
`libs/medw_core` in, so a narrower context cannot see it.

Two to expect trouble from:

- **generation** installs the Microsoft ODBC driver from `packages.microsoft.com`.
  If that apt step fails, it is the repository key or a network policy, not
  your Dockerfile.
- **reranker** bakes CPU torch plus the cross-encoder weights: ~2GB, several
  minutes, and its `requirements.txt` sets a global `--index-url` pointing at
  the PyTorch channel. Build it last and separately.

Smoke-test any image the same way retrieval was:

```bash
docker run --rm -p 18000:8000 medw-gateway:dev
```

`/healthz` should return 200 immediately; `/readyz` should return 503 until
its dependencies exist. If `/readyz` returns 200 with nothing running, the
readiness probe is checking the wrong thing.

### 3. First CI run

Pushing to `main` or opening a PR triggers `.github/workflows/ci.yml`. The
`delivery` job downloads kubeconform, renders every chart and builds the Flux
overlays; the `images` job builds four of the five services. None of it has
run on a real runner yet — expect the first run to surface something, most
likely in the image matrix.

### 4. Prove the reconcile loop

```bash
k3d cluster create medw --agents 2
kubectl create namespace medw
helm install qdrant deploy/charts/qdrant -n medw
```

Then KEDA (every service except the reranker renders a `ScaledObject` and the
CRDs must exist first), then `flux bootstrap` per `deploy/flux/README.md`.

The loop is closed when you can change an image tag in `deploy/flux/dev`,
commit, watch Flux reconcile, and `git revert` it back.

**Local-cluster caveat:** the charts assume `managed-csi-premium` (Azure) for
Qdrant's volumes and reference an nginx ingress class. On k3d you will need
`--set persistence.storageClass=local-path` and the bundled Traefik, or a
`values-local.yaml`. Do not "fix" this by changing the defaults — the defaults
describe the target environment, and a local override file is the honest way
to say so.

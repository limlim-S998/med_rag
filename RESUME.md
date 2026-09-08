# Current state

Everything on the scaffolding plan is complete. This page is the short answer
to "where is this now"; [README.md](README.md) is the orientation document and
[ROADMAP.md](ROADMAP.md) has the full history and the defect list.

```
lint OK · 6 architecture contracts · mypy clean · 147 tests
7 charts render · 5 Flux overlays build
```

## Done

| Phase | What it produced |
|---|---|
| Contracts and boundaries | 12 Protocols, error model, `import-linter` contracts in CI |
| Data contracts | `projections.py` — one source of truth for both stores |
| Delivery loop | charts, CI, versioning tests, 5 images that build and run |
| Real Azure | AOAI live; Storage, Search, Cosmos, DI, Language on free tiers |
| Composition root | `MEDW_BACKEND=local|azure`, every port with two implementations |
| Cluster proof | Flux deploy + `git revert` rollback on minikube |
| Prometheus + KEDA | 4/4 targets scraped, HPA resolves the metric and scales on it |
| Consistency model | ingestion FSM, retry economics, two-store drift detection |
| Provenance | one frozen stamp on every audit row and metric |

## Not done, and why

- **Nothing is measured.** No trained classifier, no retrieval number, no CT.
  Blocked on the held-back implementations, not on missing design.
- **Scale-*up* undemonstrated.** KEDA scales *down* on the real metric, but
  stub handlers return in microseconds so load never registers.
- **Azure SQL unprovisioned** — needs an AAD admin principal decision.
  `engine()` fails loudly rather than building a connection to nowhere.
- **Azure-backend readiness returns 503** with reason `reachability checks not
  implemented`. Honest, and it blocks an AKS deploy until written.

## The obvious next step

Reintroduce the held-back implementations one at a time from the
`implementation/retrieval-slice` branch (`089dd50`), each with its tests. That
is what turns "the platform is proven" into "the system is measured".

Start with the fixtures and `pipelines/cli.py` — everything downstream (the
golden set, `make eval`, a real recall number) is blocked on that one file.

## Local environment

```bash
make dev && make check          # from a clean checkout
minikube start -p medw          # cluster is stopped by default
./scripts/local_deploy.sh       # immutable per-build image tags
```

Azure resources live in resource group `med-rag-tester` (australiaeast) and
cost nothing at rest — every one is free-tier or pay-per-token.

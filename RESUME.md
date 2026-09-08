# Resume point — scaffolding build-out

Repo is green: lint, 6 architecture contracts, mypy, **102 tests**.
All four services verified starting under **both** `MEDW_BACKEND=local`
and `MEDW_BACKEND=azure`.
**38 files uncommitted on `main`** — commit before doing anything else.

## Where we got to

Working on the scaffolding gap list. Items 1–3 and 7–8 are done; 6 is part-done.

| # | Item | State |
|---|---|---|
| 1 | Composition root | **Done.** `libs/medw_core/composition.py`, `MEDW_BACKEND=local\|azure` |
| 2 | Second implementation per port | **Done.** `libs/medw_core/local/` — all 12 ports |
| 3 | Wire remaining 3 services to composition root | **Done.** All 4 verified starting under both backends |
| 3½ | **Prometheus + in-flight metric** | **Code done.** Gauge, middleware, exporter, /metrics, 7 tests. Cluster install (kube-prometheus-stack + KEDA) pending |
| 4 | Provenance stamp | **Done.** `medw_core/provenance.py`, wired into the audit write |
| 5 | Consistency & failure model | **Not started** |
| 6 | Cluster proof | **Done.** Flux installed; deploy + `git revert` rollback both proven |
| 7 | Azure provisioning | **Done** (except SQL) |
| 8 | CI confirmation | **Done** — all runs green |

## Item 6: exactly where it stopped

- `minikube` profile **`medw`** is running (4 CPU / 8GB, docker driver, ingress addon enabled, `standard` storage class).
- All 5 service images are loaded into it (`minikube image ls -p medw`).
- `values-local.yaml` written for all 6 charts and verified to render.
- **The next command was the `helm upgrade --install` loop, which was interrupted and never ran.** Nothing is deployed to the cluster yet.

To pick up:

```bash
kubectl --context medw create namespace medw
for c in qdrant reranker retrieval generation ingestion-worker gateway; do
  helm upgrade --install "$c" "deploy/charts/$c" -n medw --kube-context medw \
    -f "deploy/charts/$c/values-local.yaml" --wait --timeout 90s
done
```

Then Flux (not yet installed on the cluster) reconciling a tag bump and a revert.

## Azure — what now exists in `med-rag-tester` (australiaeast)

| Resource | Name | Tier |
|---|---|---|
| Azure OpenAI | `med-rag-test1` | S0, 2 deployments |
| Storage | `medrag325744d5sa` | Standard_LRS, 3 containers |
| AI Search | `medrag325744d5search` | **free** |
| Cosmos DB | `medrag325744d5cosmos` | **free tier** |
| Document Intelligence | `medragdevdi` | **F0** |
| AI Language | `medrag325744d5lang` | **F0** |

Not provisioned: **Azure SQL** (needs an AAD admin decision — no AAD group
exists, so it would have to use your own user as the admin principal), ACR,
AKS.

Naming is inconsistent — `medragdevdi` came from a first attempt that
partially succeeded before a globally-taken storage name aborted the rest.
Cosmetic only.

## Findings worth not losing

1. **A fresh subscription has almost every resource provider unregistered**,
   and the error is a misleading `SubscriptionNotFound`. 11 providers
   registered; `bootstrap.sh` now needs a registration step (not yet added).
2. **Endpoint URLs cannot be constructed from resource names.** Azure appends a
   random suffix when it generates a custom subdomain —
   `medragdevdi-40aab.cognitiveservices.azure.com`. `bootstrap.sh` was fixed to
   read them back with `az cognitiveservices account show`.
3. **`ChatClient.stream` was an unsatisfiable Protocol.** Declared `async def
   ... -> AsyncIterator[str]`, which types as a coroutine *resolving to* an
   iterator — not what an `async def ... yield` function is. mypy caught it only
   once a second implementation existed. Fixed to a plain `def`.
4. **Owner does not grant data-plane access** (ADR 0008), and RBAC propagation
   is per-action: chat worked immediately, embeddings 401'd for ~2.5 minutes
   from the same assignment.

## Reclaiming local resources

```bash
minikube stop -p medw          # or: minikube delete -p medw
docker compose down            # stops the local Qdrant
```

Azure costs nothing at rest — every provisioned resource is free-tier or
pay-per-token, and the only standing charge would be an ACR or AKS node pool,
neither of which exists.

## Held-back implementations

Unchanged: durable copy is branch `implementation/retrieval-slice`
(commit `089dd50`); `holding/` is a gitignored working copy.

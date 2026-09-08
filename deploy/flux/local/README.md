# Proving the reconcile loop locally

Flux reads from **git**, not from your working tree. Everything below assumes
the change is committed and pushed — that constraint is the whole point, and
it is why a local Flux run needs its own committed overlay.

## Prerequisites

```bash
sudo pacman -S fluxcd          # official Arch repo, 2.9.x
```

The repo is public, so Flux clones it with no secret and no token.

## Install the controllers

`flux bootstrap` is the usual entry point, but it commits its own controller
manifests into your repo and needs a GitHub PAT. For a throwaway local cluster
that is more ceremony than it is worth:

```bash
flux install --context medw
```

## Point Flux at the repo

```bash
kubectl --context medw apply -f - <<'YAML'
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: { name: medwriter-assist, namespace: flux-system }
spec:
  interval: 1m
  url: https://github.com/limlim-S998/med_rag
  ref: { branch: main }
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: { name: medw-local, namespace: flux-system }
spec:
  interval: 1m
  path: ./deploy/flux/local
  prune: true
  sourceRef: { kind: GitRepository, name: medwriter-assist }
YAML
```

## Prove it

```bash
flux get all -A                       # sources and releases reconciling
kubectl -n medw get pods
```

Then the loop itself — **three commits, and the middle one is the deploy**:

```bash
# 1. change the image tag in deploy/flux/local/kustomization.yaml
git commit -am "deploy(local): gateway -> <new tag>" && git push
flux reconcile source git medwriter-assist   # or wait 1m
kubectl -n medw get pods -w                  # watch the rollout

# 2. roll it back
git revert --no-edit HEAD && git push
flux reconcile source git medwriter-assist
```

The revert is the demonstration. Nobody ran `helm rollback`; the cluster
returned to the previous state because git did.

## What this does NOT prove

- **Workload identity.** There is no AAD to federate against, so every service
  runs with `MEDW_BACKEND=local` and no credential. That is an AKS-only
  exercise.
- **KEDA autoscaling.** The CRDs are not installed and `values-local.yaml`
  disables it. See ROADMAP C¾ — the trigger currently queries a metric nothing
  emits, so installing KEDA alone would not help.
- **Ingress TLS.** No cert-manager locally; the local overlay sets `tls: false`.

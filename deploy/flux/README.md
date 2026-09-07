# flux

## What was wrong before

The previous version of this directory listed Helm chart *directories* as
kustomize `resources:`, and patched `kind: HelmRelease` objects that did not
exist anywhere in the repo. Kustomize cannot consume a chart directory as a
resource, so `kustomize build` would have failed outright. It looked like
GitOps and would never have reconciled anything.

It is worth knowing why that mistake is easy: `helm template` succeeding tells
you the charts are fine, and nothing checks that the thing *pointing* at the
charts is fine. The charts were never the problem.

## How it actually works

Flux needs three kinds of object, and the split between them is the design:

1. **`GitRepository`** — a source. "Watch this repo at this branch, every
   minute." One per cluster, in `flux-system`.
2. **`HelmRelease`** — one per service. Says which chart path inside that
   source to install, with which values. This is the object Helm-based GitOps
   actually revolves around, and it was entirely missing.
3. **`Kustomization`** (Flux's, not kustomize's) — points Flux at the
   directory of `HelmRelease` manifests for an environment.

```
deploy/flux/
├── base/                 the HelmReleases, environment-agnostic
│   ├── kustomization.yaml
│   └── *.yaml            one HelmRelease per service
├── dev/                  base + dev values
├── staging/              base + staging values
└── prod/                 base + prod values
```

Each environment overlays the same base and patches only what differs. That is
what makes promotion "a PR that moves the same image tag up an environment"
rather than a rebuild.

## The versioning loop, end to end

1. Merge to `main` → Azure Pipelines builds and pushes `service:<git-sha>`.
2. The pipeline's last act is a commit bumping `image.tag` in the dev overlay.
3. Flux notices the commit within its interval and reconciles the cluster.
4. Promotion to staging is a PR copying that tag into the staging overlay. The
   artifact is never rebuilt, so what shipped is bit-for-bit what was tested.
5. Rollback is `git revert` on the bump commit.

The git log is the deployment history. Nobody runs `helm upgrade` by hand and
nobody has write access to prod with kubectl — if they did, the log would be
a record of intentions rather than of what happened.

## Bootstrapping (needs a cluster)

```bash
flux bootstrap github --owner=<you> --repository=medwriter-assist \
  --branch=main --path=deploy/flux/dev --personal
```

`flux bootstrap` commits its own controller manifests into the repo, so the
thing that runs the GitOps loop is itself under GitOps.

## Cluster prerequisites

Not optional, and both fail loudly rather than silently:

- **KEDA** — every service except the reranker scales on a `ScaledObject`.
  Without the CRDs the release fails to install.
- **An ingress controller** — the gateway renders an Ingress with nginx
  annotations, including `proxy-buffering: off`, which streaming depends on.

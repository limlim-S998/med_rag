# Versioning

Four things can change what this system outputs. Each needs its own
identifier, or a regression cannot be attributed to anything.

| Axis | Identifier | Lives in | Enforced by |
|---|---|---|---|
| **Code** | git SHA | `image.tag` in each chart's `values.yaml` | `test_no_chart_uses_a_floating_tag`, `validate_tag()` |
| **Model** | AOAI deployment name + version, `embed_version`, `prompt_bundle_sha` | `config:` in `values.yaml` | `test_chat_deployment_carries_a_version_suffix`, `test_indexer_and_retriever_agree_on_the_embedding_version` |
| **Deployment** | the commit that changed the values file | git history | Flux reconciles to a commit; nothing else applies |
| **Chart** | `medw-lib` semver + consumer pins | `Chart.yaml`, `Chart.lock` | `test_library_chart_pin_matches_the_library_version` |

The reason these are tests and not conventions: **every one of them fails
silently.** An unpinned model deployment does not error — Azure rolls the
version forward and the outputs simply change. A stale chart pin does not
error — the service renders from an old vendored copy. Silent failures need a
loud check or they are not managed at all.

## Why the image tag is a git SHA and never `latest`

`latest` makes "what is running in prod" unanswerable and rollback
impossible. `scripts/bump_image_tag.py` refuses it outright, along with
anything else that is not a git SHA — a tag that cannot be traced to a commit
cannot be traced to source.

## Why environment overlays never set their own tag

Promotion is a PR that moves the *same* SHA up an environment. The artifact is
never rebuilt, so what ships is bit-for-bit what was tested. If
`values-prod.yaml` could pin its own tag, that guarantee would quietly stop
being true — so a test asserts the overlays override `image.repository` (prod
uses a different ACR) but never `image.tag`.

## Why `embed_version` is checked across charts

It is baked into the Qdrant collection name. If ingestion writes with
`v3l-001` and retrieval queries with `v3l-002`, the collection names differ
and retrieval searches something ingestion never wrote to. No error — just
empty or stale results. It is the one invariant that cannot be expressed
inside a single chart, so it gets its own test.

## The release step

The pipeline's last act is a commit, not a deploy:

```bash
make release DRY=1      # preview
make release            # set every image tag to HEAD
```

This was a `yq -i` one-liner inside Azure Pipelines. `yq` was installed
nowhere in this repo, so the most consequential step in the delivery path
could only ever run on a build agent — untestable, and with no way to preview
what it would change. It is now `scripts/bump_image_tag.py`: it validates the
tag, edits exactly one line, preserves the comments that explain every setting
in the values files, and has coverage in `tests/test_versioning.py`.

## Bumping the library chart

`medw-lib` is a dependency of all five service charts, pinned by version.
Changing it means bumping it *and* re-pinning every consumer:

```bash
# 1. edit deploy/charts/medw-lib/Chart.yaml   -> version: 0.5.0
# 2. edit each consumer's Chart.yaml dependency pin
# 3. re-resolve, which rewrites Chart.lock
make charts
```

Skipping step 2 leaves that service rendering from a stale vendored tarball.
That has already happened once — `medw-lib` gained the autoscaling, ingress
and PDB templates and every consumer silently kept rendering the 0.3.0 copy.
`test_library_chart_pin_matches_the_library_version` now catches it at commit
time rather than at deploy time.

`Chart.lock` is committed; `charts/*.tgz` is not. The lock pins what was
resolved — the same argument as committing a dependency lockfile. The tarball
is a build artifact and would put a binary diff in front of every reviewer.

## What is not versioned yet

- **`appVersion`** is `0.1.0` on every service chart and means nothing.
  It should track the application release once there is one.
- **The Python packages** (`medwriter-assist`, `medw-core`) are both `0.1.0`,
  hand-edited in two files. Nothing consumes those versions today — the
  services install `medw_core` from the local path, not from an index — so
  this is latent rather than broken.
- **No git tags or releases.** The git SHA is the only artifact identifier,
  which is sufficient while nothing is published.
- **Registered model versions** (`table_classifier_version`) are pinned in
  values, but nothing has been trained, so the pin points at nothing.

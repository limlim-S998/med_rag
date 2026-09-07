# ADR 0007: the Azure OpenAI deployment tier is a data-residency decision, not a pricing one.

**Status:** accepted — and discovered the hard way, in this repo, on a real
subscription.

## Context

Azure OpenAI deployments have a SKU: `Standard`, `DataZoneStandard`,
`GlobalStandard`, `GlobalBatch`, and provisioned variants. It is natural to
read that list as a price/throughput ladder and pick whichever has capacity.

It is not only that. The tier determines **where inference physically
happens**:

- `Standard` — served from the region the resource lives in.
- `DataZoneStandard` — served within a defined data boundary (e.g. EU).
- `GlobalStandard` — routed through Microsoft's global capacity pool.
  Throughput and availability are better because the pool is larger; the
  request may leave the region.

For this system, that is not a footnote. The brief's constraint is EU data
residency with a no-training guarantee under a BAA, and the corpus is
unpublished clinical trial data. A `GlobalStandard` deployment of the chat
model would mean prompts assembled from patient-level tables leaving the
residency boundary — which is a contractual breach, not a latency trade.

## Decision

Deployment SKU is treated as a residency control and pinned in Helm values
alongside the deployment name and version, not chosen for throughput.

For any environment with a residency constraint: `Standard` or
`DataZoneStandard` only. `GlobalStandard` is permissible only where the data
is synthetic.

## Consequence, observed

On the learning subscription used to build this out, the quota is:

```
OpenAI.Standard.gpt-4o                    limit 0
OpenAI.Standard.text-embedding-3-large    limit 350   (thousand TPM)
OpenAI.GlobalStandard.gpt4.1-mini         limit 200
OpenAI.GlobalStandard.gpt-5-mini          limit 500
```

There is **no Standard chat quota at all**. Every chat model that can be
deployed is `GlobalStandard`. Embeddings, by contrast, are available on
`Standard` and are deployed that way.

So the residency-safe tier was not available for the half of the workload that
sees the most sensitive text. On a trial subscription with synthetic data that
is acceptable and is recorded here as a known deviation. On a client
subscription it would be a blocker to raise before writing any code — quota
for the correct tier is a procurement question with a lead time, not something
to discover during integration.

## What this changes in the repo

- `infra/bootstrap.sh` deploys embeddings on `Standard` and chat on
  `GlobalStandard`, with the reason stated at the call site.
- The model catalogue and the quota are checked separately before a deployment
  name is written down. `gpt-4o` appears in `list-models` for this region *and*
  has a Standard limit of zero — the failure is a quota error, not a
  not-found, and the two look nothing alike.

## Also worth recording

`--sku-capacity` is a **rate limit, not a reservation**. Billing is per token
regardless, so a low capacity costs nothing and bounds how fast anything can
burn credit. Deployments here are created at 10 (10k TPM) for that reason.

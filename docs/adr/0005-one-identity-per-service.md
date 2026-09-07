# ADR 0005: one managed identity per service, not one per cluster.

**Status:** accepted

**Decision.** Each service gets its own user-assigned managed identity, its own
federated credential bound to its own Kubernetes service account, and only the
role assignments it needs. The reranker's identity holds no roles at all.

**Why.** A shared identity gives every pod the union of every permission, and
the union is always the widest one. Under a shared identity, a bug in the
retrieval request path can write to Blob and insert into the audit trail —
neither of which retrieval has any reason to do.

**Cost.** Five identities, five federated credentials, five sets of role
assignments in `bootstrap.sh`, and one more thing to get wrong when adding a
service. The failure mode is loud (`DefaultAzureCredential` fails immediately),
which is what makes the cost tolerable.

**Consequence worth noting.** Cosmos data-plane access does not go through
`az role assignment create` — it has a separate role family with its own
command. The control plane and data plane being different RBAC systems is not
obvious, and it is where this decision costs the most time.

# ADR 0008: data-plane RBAC is a separate system from control-plane RBAC, and it propagates unevenly.

**Status:** accepted — observed directly while wiring this repo to a real
Azure OpenAI resource.

## Context

Azure has two distinct permission systems on the same resource:

- **Control plane** — create, configure, scale, delete. `Owner`,
  `Contributor`, `Reader`. Expressed as `actions`.
- **Data plane** — call the thing. Read a document, run an inference.
  Expressed as `dataActions`, granted by different roles entirely.

Nothing about being `Owner` grants data-plane access, and the failure is not
obvious from the role name.

## The observation

Authenticated as **subscription Owner**, with the deployments created
successfully by that same identity, an embeddings call returned:

```
401 PermissionDenied
The principal `d453e6a4-...` lacks the required data action
Microsoft.CognitiveServices/accounts/OpenAI/deployments/embeddings/action
```

Owner could create the deployment and could not use it. The fix is a separate
assignment of `Cognitive Services OpenAI User`, scoped to the resource:

```bash
az role assignment create --assignee <oid> \
  --role "Cognitive Services OpenAI User" \
  --scope <aoai-resource-id>
```

This is the same split already recorded for Cosmos in ADR 0005, where the data
plane has its own role family and its own command
(`az cosmosdb sql role assignment create`, not `az role assignment create`).
Two services, same architecture, different CLI surface — which is why it
catches people twice.

## The second observation, which is the more useful one

After the assignment landed, **chat worked immediately and embeddings kept
failing for roughly two and a half minutes** — six failed attempts, twenty-five
seconds apart, from the *same* role assignment on the *same* resource.

Propagation is eventually consistent and evidently per-action rather than
per-assignment. Two consequences:

1. **Testing one operation does not prove the grant.** Had we checked only
   chat, we would have called it fixed and hit the embeddings failure later,
   under indexing load, where it would have looked like a bug in the pipeline.
2. **The error message degrades as it propagates.** Before the assignment
   existed the message named the exact missing data action. Afterwards it
   became the vague `Principal does not have access to API/Operation`. So:

   - *specific message naming a data action* → a role is genuinely missing
   - *vague message* → the grant exists, wait and retry

   Reading those as the same error sends you to re-check RBAC that is already
   correct.

## Decision

- Every identity gets its data-plane role explicitly. Control-plane role
  assignments are never assumed to imply data access — including for humans at
  a terminal, not just workload identities.
- Anything that provisions a resource and then immediately calls it retries on
  401 for a few minutes before concluding the grant is wrong.
- `infra/bootstrap.sh` assigns the signed-in user `Cognitive Services OpenAI
  User` as an explicit step, with the propagation behaviour noted at the call
  site.

## Consequence

This is the mechanism behind the brief's claim that the Azure learning curve
is "almost entirely which role grants which action". The version that actually
costs time is not looking up a role name — it is that the most privileged role
in the subscription silently does not include the one action you need.

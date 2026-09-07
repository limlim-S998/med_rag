#!/usr/bin/env bash
# What "using Azure" actually feels like. Read this top to bottom once and the
# portal stops being mysterious - the portal is just a GUI over these calls.
#
# Mental model:
#   subscription > resource group > resource
# A resource group is a folder with a lifecycle. Delete the group, everything
# in it goes. That is why per-environment groups are the default.

set -euo pipefail

# Region. The client system ran in westeurope because EU data residency was a
# hard requirement; this learning environment runs in australiaeast because
# that is where the machine is. Parameterised rather than edited, so the
# residency constraint stays visible instead of being quietly overwritten.
LOC=${MEDW_LOCATION:-australiaeast}
ENVN=${MEDW_ENV:-dev}
RG=rg-medw-$ENVN
PREFIX=medw$ENVN

# FREE=1 provisions the cheapest shape of everything that has a free tier.
# Default, because the expensive shapes bill from the moment they exist and
# nothing here needs them.
FREE=${FREE:-1}

az login
az account set --subscription "$AZ_SUBSCRIPTION_ID"
az group create -n $RG -l $LOC

# Budget alert FIRST, before anything billable exists. A budget created after
# the fact is a budget created after the mistake.
az consumption budget create --budget-name medw-guardrail --amount 10 \
  --category Cost --time-grain Monthly \
  --start-date "$(date +%Y-%m-01)" --end-date "$(date -d '+1 year' +%Y-%m-01)" \
  2>/dev/null || echo "budget API unavailable on this subscription type - set one in the portal"

# --- Azure OpenAI -------------------------------------------------------
# Two levels: the *resource* (an endpoint + quota) and *deployments* inside it
# (named instances of a model). You call the deployment name.
az cognitiveservices account create \
  -n ${PREFIX}aoai -g $RG -l $LOC --kind OpenAI --sku S0 \
  --custom-domain ${PREFIX}aoai

# Check what you can ACTUALLY deploy before writing a deployment name down.
# The model catalogue and your quota are different things: gpt-4o appears in
# `list-models` for this region and has a Standard quota limit of 0, so the
# create call fails with a quota error rather than a not-found one.
#
#   az cognitiveservices account list-models -n ${PREFIX}aoai -g $RG -o table
#   az cognitiveservices usage list -l $LOC --query "[?limit>\`0\`]" -o table
#
# --sku-capacity is a RATE LIMIT, not a reservation. You pay per token either
# way, so a low capacity costs nothing and caps how fast anything can burn
# credit. 10 = 10k tokens/minute, ample for one writer.

az cognitiveservices account deployment create \
  -n ${PREFIX}aoai -g $RG \
  --deployment-name gpt-4.1-mini-2025-04-14 \
  --model-name gpt-4.1-mini --model-version 2025-04-14 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 10

az cognitiveservices account deployment create \
  -n ${PREFIX}aoai -g $RG \
  --deployment-name text-embedding-3-large-1 \
  --model-name text-embedding-3-large --model-version 1 --model-format OpenAI \
  --sku-name Standard --sku-capacity 10

# Two things in those calls are load-bearing.
#
# The deployment name carries the version. Name it "gpt-4.1-mini" and Azure
# will roll the underlying version forward and your outputs will change with
# no commit anywhere in your repo.
#
# The SKU is Standard for embeddings and GlobalStandard for chat, and that is
# not a pricing preference - it is where inference physically happens. See
# docs/adr/0007. On this subscription there is no Standard chat quota at all,
# so the residency-safe tier was simply not available.

# --- storage ------------------------------------------------------------
# Three containers, three lifecycles. See libs/medw_core/blob.py.
az storage account create -n ${PREFIX}sa -g $RG -l $LOC --sku Standard_LRS
for c in raw parsed snapshots; do
  az storage container create --account-name ${PREFIX}sa -n $c --auth-mode login
done

# --- search -------------------------------------------------------------
# Free tier: small, permanent, one per subscription. `standard` bills from
# the moment it exists and nothing here needs it.
az search service create -n ${PREFIX}search -g $RG -l $LOC \
  --sku $([ "$FREE" = 1 ] && echo free || echo standard)
# The index is a JSON definition, not a CLI flag - analyzers, scoring profile
# and the BM25 parameters all live in it.
az rest --method put \
  --uri "https://${PREFIX}search.search.windows.net/indexes/csr-chunks?api-version=2024-07-01" \
  --resource https://search.azure.com \
  --body @infra/search/csr-chunks-index.json

# --- state: Cosmos ------------------------------------------------------
# Serverless in dev (you pay per request and it idles at zero); autoscale
# throughput in prod. The partition key is fixed at creation - it is the one
# choice here that costs a migration to undo.
# Free tier gives 1000 RU/s + 25GB permanently, one per subscription.
az cosmosdb create -n ${PREFIX}cosmos -g $RG --locations regionName=$LOC \
  $([ "$FREE" = 1 ] && echo --enable-free-tier true || echo --capabilities EnableServerless)
az cosmosdb sql database create -a ${PREFIX}cosmos -g $RG -n medw

az cosmosdb sql container create -a ${PREFIX}cosmos -g $RG -d medw \
  -n documents  --partition-key-path /study_id
az cosmosdb sql container create -a ${PREFIX}cosmos -g $RG -d medw \
  -n jobs       --partition-key-path /study_id --ttl 2592000
az cosmosdb sql container create -a ${PREFIX}cosmos -g $RG -d medw \
  -n sessions   --partition-key-path /user_id  --ttl 43200
az cosmosdb sql container create -a ${PREFIX}cosmos -g $RG -d medw \
  -n generations --partition-key-path /study_id --ttl 7776000

# --- state: Azure SQL ---------------------------------------------------
# No SQL admin password anywhere. AAD-only auth, an AAD group as the server
# administrator, and each workload identity is a contained user created FROM
# EXTERNAL PROVIDER (db/sql/0003_grants.sql).
az sql server create -n ${PREFIX}sql -g $RG -l $LOC \
  --enable-ad-only-auth --external-admin-principal-type Group \
  --external-admin-name "sg-medw-sql-admins" \
  --external-admin-sid "$AAD_SQL_ADMIN_GROUP_OBJECT_ID"
# Free serverless offer auto-pauses when idle. S1 bills continuously.
az sql db create -n medw -s ${PREFIX}sql -g $RG \
  $([ "$FREE" = 1 ] \
    && echo '--edition GeneralPurpose --compute-model Serverless --family Gen5 --capacity 1 --use-free-limit --free-limit-exhaustion-behavior AutoPause' \
    || echo '--service-objective S1')
az sql server firewall-rule create -n allow-azure -s ${PREFIX}sql -g $RG \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0   # "Azure services", not the world

# --- document parsing + clinical NER ------------------------------------
# F0 is the free tier for both: a monthly page/record cap, no standing cost.
# One F0 account per kind per subscription. Plenty for a synthetic study;
# S0/S bill per page and per record from the first call.
AI_SKU=$([ "$FREE" = 1 ] && echo F0 || echo S0)
az cognitiveservices account create -n ${PREFIX}di -g $RG -l $LOC \
  --kind FormRecognizer --sku $AI_SKU     # Document Intelligence, old CLI name
az cognitiveservices account create -n ${PREFIX}lang -g $RG -l $LOC \
  --kind TextAnalytics --sku $AI_SKU      # Azure AI Language: healthcare NER + UMLS

# --- Azure ML: experiment tracking + the model registry -----------------
# The registry earns its keep on the models that have weights we trained -
# the sklearn classifiers. A hosted model has no artifact to register, which
# is why its version lives in a Helm value instead.
az ml workspace create -n medw-${ENVN}-ws -g $RG -l $LOC
az ml compute create -n cpu-cluster -g $RG -w medw-${ENVN}-ws \
  --type AmlCompute --min-instances 0 --max-instances 4 --size Standard_DS3_v2

# --- observability ------------------------------------------------------
az monitor log-analytics workspace create -n ${PREFIX}logs -g $RG -l $LOC
az monitor app-insights component create -a ${PREFIX}ai -g $RG -l $LOC \
  --workspace $(az monitor log-analytics workspace show -n ${PREFIX}logs -g $RG --query id -o tsv)
# The connection string goes into Helm values. It is not a secret in the
# credential sense - it is an ingestion key with write-only telemetry scope.

# --- container registry + cluster ---------------------------------------
# Basic is the cheapest ACR tier and the only standing charge in this file.
# It is small, but it is per-month whether or not you push anything - so it
# is the first thing to delete between sessions.
az acr create -n ${PREFIX}acr -g $RG --sku Basic
az aks create -n ${PREFIX}aks -g $RG \
  --node-count 3 --node-vm-size Standard_D4s_v5 \
  --attach-acr ${PREFIX}acr \
  --enable-oidc-issuer --enable-workload-identity \
  --enable-managed-identity --generate-ssh-keys

az aks get-credentials -n ${PREFIX}aks -g $RG    # writes ~/.kube/config

# --- workload identity: the part worth understanding --------------------
# Goal: a pod calls Azure OpenAI with no secret anywhere in the cluster.
#
#   1. Make a user-assigned managed identity (an AAD principal you own).
#   2. Give it RBAC on the resources it needs.
#   3. Federate it to a specific k8s service account in a specific namespace.
#   4. Annotate that service account with the identity's client ID.
#
# Then DefaultAzureCredential inside the pod finds the projected token file,
# swaps it for an AAD token, and every SDK client just works.

IDENT=id-medw-retrieval
az identity create -n $IDENT -g $RG
CLIENT_ID=$(az identity show -n $IDENT -g $RG --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -n $IDENT -g $RG --query principalId -o tsv)
ISSUER=$(az aks show -n ${PREFIX}aks -g $RG --query oidcIssuerProfile.issuerUrl -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role "Cognitive Services OpenAI User" \
  --scope $(az cognitiveservices account show -n ${PREFIX}aoai -g $RG --query id -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role "Storage Blob Data Reader" \
  --scope $(az storage account show -n ${PREFIX}sa -g $RG --query id -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role "Search Index Data Reader" \
  --scope $(az search service show -n ${PREFIX}search -g $RG --query id -o tsv)

az identity federated-credential create \
  --name fc-retrieval --identity-name $IDENT -g $RG \
  --issuer "$ISSUER" \
  --subject system:serviceaccount:medw:retrieval \
  --audience api://AzureADTokenExchange

# --- data plane is NOT the control plane --------------------------------
# Owner on the subscription does not let you call a model. It is a control
# plane role: create, configure, delete. Calling an inference endpoint needs
# a data action, and only these roles carry it. This applies to YOU at a
# terminal exactly as much as it applies to a pod. See docs/adr/0008.
MY_OID=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee $MY_OID \
  --role "Cognitive Services OpenAI User" \
  --scope $(az cognitiveservices account show -n ${PREFIX}aoai -g $RG --query id -o tsv)
# Propagation is uneven and per-action: chat answered immediately while
# embeddings 401'd for ~2.5 minutes off the SAME assignment. Anything that
# provisions then immediately calls needs a retry loop.

echo "serviceAccount.annotations: azure.workload.identity/client-id: $CLIENT_ID"
# ^ that value goes into values-dev.yaml. It is a client ID, not a secret.

# --- one identity per service, not one for the cluster ------------------
# Repeat the four steps above per service. It is more lines, and it means a
# compromised retrieval pod cannot write to Blob, cannot touch Cosmos and
# cannot insert into the audit trail. A shared identity gives every pod the
# union of every permission, and the union is always the widest one.
#
#   id-medw-retrieval  AOAI User, Blob Data Reader, Search Index Data Reader
#   id-medw-generation AOAI User, + INSERT on audit via a contained SQL user
#   id-medw-ingestion  AOAI User, Blob Data CONTRIBUTOR, Search Index Data
#                      Contributor, Cognitive Services User (DI + Language),
#                      Cosmos data contributor
#   id-medw-gateway    Blob Delegator (to mint user-delegation SAS), Cosmos
#                      data contributor
#   id-medw-reranker   nothing. It calls no Azure service.

# --- the Cosmos gotcha --------------------------------------------------
# Cosmos data-plane access is NOT `az role assignment create`. The control
# plane (create/delete containers) uses ordinary Azure RBAC; reading and
# writing items uses a separate role family with its own command. Assigning
# "Cosmos DB Account Contributor" and expecting to read documents is a
# half-day of confusion that everyone has once.
COSMOS_ID=$(az cosmosdb show -n ${PREFIX}cosmos -g $RG --query id -o tsv)
az cosmosdb sql role assignment create -a ${PREFIX}cosmos -g $RG \
  --role-definition-id "$COSMOS_ID/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002" \
  --principal-id $PRINCIPAL_ID --scope "/"     # built-in Data Contributor

# --- demo environment (Container Apps, scales to zero) ------------------
# Retained after the move to AKS for one property AKS does not have: an idle
# demo costs nothing. See deploy/container-apps/demo.yaml.
az containerapp env create -n medw-demo-env -g $RG -l $LOC

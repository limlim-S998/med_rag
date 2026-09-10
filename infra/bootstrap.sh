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

# FREE selects the requested trial/demo SKUs where available. This script
# also creates a three-node AKS cluster and other commissioned resources.
# Infrastructure costs, tier eligibility, quotas and residency must be
# reviewed separately; FREE=1 is not a promise of a zero-cost environment.
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
# The model catalogue and your subscription's deployment quota are separate.
# An available model can still fail deployment because the requested quota
# or SKU is unavailable.
#
#   az cognitiveservices account list-models -n ${PREFIX}aoai -g $RG -o table
#   az cognitiveservices usage list -l $LOC --query "[?limit>\`0\`]" -o table
#
# Capacity units are model/SKU specific. Review the checked-in deployment
# request against available quota and the commissioned workload.

AOAI_ID=$(az cognitiveservices account show -n "${PREFIX}aoai" -g "$RG" --query id -o tsv)
# The installed CLI has no version-upgrade flag. Use the documented ARM
# deployment property, with model version and policy in the SAME creation PUT.
az rest --method put \
  --url "https://management.azure.com${AOAI_ID}/deployments/gpt-4.1-mini-2025-04-14?api-version=2024-10-01" \
  --body @infra/aoai/chat-deployment.json
az rest --method put \
  --url "https://management.azure.com${AOAI_ID}/deployments/text-embedding-3-large-1?api-version=2024-10-01" \
  --body @infra/aoai/embed-deployment.json

# Two things in those calls are load-bearing.
#
# A name is only a label. The actual model revision and NoAutoUpgrade policy
# above are independently checked by runtime model-identity readiness.
#
# The SKU is Standard for embeddings and GlobalStandard for chat, and that is
# relevant to where inference can happen. Review residency against the
# deployment type before commissioning; see README.md#azure-commissioning.

# --- storage ------------------------------------------------------------
# Three containers, three lifecycles. See libs/medw_core/blob.py.
az storage account create -n ${PREFIX}sa -g $RG -l $LOC --sku Standard_LRS
for c in raw parsed snapshots; do
  az storage container create --account-name ${PREFIX}sa -n $c --auth-mode login
done

# --- search -------------------------------------------------------------
# Select the requested demo or Standard SKU; validate tier limits and
# availability for the intended workload before commissioning.
az search service create -n ${PREFIX}search -g $RG -l $LOC \
  --sku $([ "$FREE" = 1 ] && echo free || echo standard)
# The index is a JSON definition, not a CLI flag - analyzers, scoring profile
# and the BM25 parameters all live in it.
az rest --method put \
  --uri "https://${PREFIX}search.search.windows.net/indexes/csr-chunks?api-version=2024-07-01" \
  --resource https://search.azure.com \
  --body "$(python3 infra/search_payload.py infra/search/csr-chunks-index.json)"

# --- state: Cosmos ------------------------------------------------------
# FREE requests a free-tier account; otherwise this script requests
# serverless. The partition key is fixed at creation, so changing it requires
# a migration. Review account eligibility and capacity before commissioning.
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
az cosmosdb sql container create -a ${PREFIX}cosmos -g $RG -d medw \
  -n platform-state --partition-key-path /study_id
# No TTL: jobs/checkpoints, source/citation evidence and index manifests are
# durable. Serving-index cleanup never deletes their evidence.

# --- state: Azure SQL ---------------------------------------------------
# No SQL admin password anywhere. AAD-only auth, an AAD group as the server
# administrator, and each workload identity is a contained user created FROM
# EXTERNAL PROVIDER (db/sql/0003_grants.sql).
az sql server create -n ${PREFIX}sql -g $RG -l $LOC \
  --enable-ad-only-auth --external-admin-principal-type Group \
  --external-admin-name "sg-medw-sql-admins" \
  --external-admin-sid "$AAD_SQL_ADMIN_GROUP_OBJECT_ID"
# Keep SQL on TCP 1433, matching the application NetworkPolicy. Azure-origin
# clients otherwise use Redirect under the Default policy, requiring extra
# destination ports. Proxy trades some throughput/latency for fixed-port egress.
az sql server conn-policy update -s ${PREFIX}sql -g $RG --connection-type Proxy
# Select the requested serverless offer or S1; verify offer eligibility and
# availability before commissioning.
az sql db create -n medw -s ${PREFIX}sql -g $RG \
  $([ "$FREE" = 1 ] \
    && echo '--edition GeneralPurpose --compute-model Serverless --family Gen5 --capacity 1 --use-free-limit --free-limit-exhaustion-behavior AutoPause' \
    || echo '--service-objective S1')
az sql server firewall-rule create -n allow-azure -s ${PREFIX}sql -g $RG \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0   # "Azure services", not the world

# --- document parsing + clinical NER ------------------------------------
# Select F0 for the demo request or S0 otherwise. Validate each service's
# SKU limits and regional availability before commissioning.
AI_SKU=$([ "$FREE" = 1 ] && echo F0 || echo S0)
az cognitiveservices account create -n ${PREFIX}di -g $RG -l $LOC \
  --kind FormRecognizer --sku $AI_SKU     # Document Intelligence, old CLI name
az cognitiveservices account create -n ${PREFIX}lang -g $RG -l $LOC \
  --kind TextAnalytics --sku $AI_SKU      # Azure AI Language: healthcare NER + UMLS

# --- read the endpoints back; they cannot be constructed ----------------
# Azure appends a random suffix when it generates a custom subdomain, so a
# Document Intelligence account named `medwdevdi` answers on something like
# https://medwdevdi-40aab.cognitiveservices.azure.com/. Building that URL from
# the resource name gives a hostname that does not resolve, and the failure
# looks like a network problem rather than a naming one.
for acct in ${PREFIX}di ${PREFIX}lang; do
  az cognitiveservices account show -n $acct -g $RG --query properties.endpoint -o tsv
done
# Feed those into the Helm values; do not hand-write them.

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
# AKS uses Azure CNI Overlay with Cilium so the chart's Kubernetes
# NetworkPolicies are enforced. Review these ranges against the node subnet,
# connected networks and each other before commissioning.
POD_CIDR=${MEDW_POD_CIDR:-192.168.0.0/16}
SERVICE_CIDR=${MEDW_SERVICE_CIDR:-10.0.0.0/16}
DNS_SERVICE_IP=${MEDW_DNS_SERVICE_IP:-10.0.0.10}
az acr create -n ${PREFIX}acr -g $RG --sku Basic
az aks create -n ${PREFIX}aks -g $RG \
  --node-count 3 --node-vm-size Standard_D4s_v5 \
  --network-plugin azure --network-plugin-mode overlay --network-dataplane cilium \
  --pod-cidr "$POD_CIDR" --service-cidr "$SERVICE_CIDR" --dns-service-ip "$DNS_SERVICE_IP" \
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

ISSUER=$(az aks show -n "${PREFIX}aks" -g "$RG" --query oidcIssuerProfile.issuerUrl -o tsv)
COSMOS_ID=$(az cosmosdb show -n "${PREFIX}cosmos" -g "$RG" --query id -o tsv)
STORAGE_ID=$(az storage account show -n "${PREFIX}sa" -g "$RG" --query id -o tsv)
SEARCH_ID=$(az search service show -n "${PREFIX}search" -g "$RG" --query id -o tsv)
COSMOS_READER="$COSMOS_ID/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001"
COSMOS_WRITER="$COSMOS_ID/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
EVIDENCE_APPENDER=$(az cosmosdb sql role definition create -a "${PREFIX}cosmos" -g "$RG" \
  --body @infra/cosmos/evidence-appender.json --query id -o tsv)
PLATFORM_WRITER=$(az cosmosdb sql role definition create -a "${PREFIX}cosmos" -g "$RG" \
  --body @infra/cosmos/platform-writer.json --query id -o tsv)

grant_resource() {
  az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal --role "$1" --scope "$2"
}

grant_cosmos() {
  az cosmosdb sql role assignment create -a "${PREFIX}cosmos" -g "$RG" \
    --principal-id "$PRINCIPAL_ID" --role-definition-id "$1" \
    --scope "$COSMOS_ID/dbs/medw/colls/$2"
}

# Exact runtime access map. Generation only creates retention markers; it
# cannot replace/delete source evidence or the active index pointer. The
# writer's CAS updates require replace, but it still gets no state delete.
for SERVICE in gateway retrieval generation ingestion-worker reranker qdrant-backup; do
  ID_SUFFIX=$SERVICE
  if [ "$SERVICE" = ingestion-worker ]; then ID_SUFFIX=ingestion; fi
  IDENT="id-medw-$ID_SUFFIX"
  az identity create -n "$IDENT" -g "$RG"
  CLIENT_ID=$(az identity show -n "$IDENT" -g "$RG" --query clientId -o tsv)
  PRINCIPAL_ID=$(az identity show -n "$IDENT" -g "$RG" --query principalId -o tsv)
  az identity federated-credential create --name "fc-$SERVICE" --identity-name "$IDENT" -g "$RG" \
    --issuer "$ISSUER" --subject "system:serviceaccount:medw:$SERVICE" \
    --audience api://AzureADTokenExchange
  case "$SERVICE" in
    retrieval)
      grant_resource "Cognitive Services OpenAI User" "$AOAI_ID"
      grant_resource "Reader" "$AOAI_ID"
      grant_resource "Search Index Data Reader" "$SEARCH_ID"
      grant_cosmos "$COSMOS_READER" platform-state
      ;;
    generation)
      grant_resource "Cognitive Services OpenAI User" "$AOAI_ID"
      grant_resource "Reader" "$AOAI_ID"
      grant_resource "Storage Blob Data Reader" "$STORAGE_ID/blobServices/default/containers/raw"
      grant_cosmos "$EVIDENCE_APPENDER" platform-state
      ;;
    ingestion-worker)
      grant_resource "Cognitive Services OpenAI User" "$AOAI_ID"
      grant_resource "Reader" "$AOAI_ID"
      grant_resource "Search Index Data Contributor" "$SEARCH_ID"
      grant_resource "Storage Blob Data Contributor" "$STORAGE_ID/blobServices/default/containers/raw"
      grant_resource "Storage Blob Data Contributor" "$STORAGE_ID/blobServices/default/containers/parsed"
      grant_cosmos "$PLATFORM_WRITER" platform-state
      grant_cosmos "$COSMOS_WRITER" documents
      ;;
    gateway)
      grant_cosmos "$COSMOS_WRITER" sessions
      grant_cosmos "$COSMOS_READER" documents
      ;;
    qdrant-backup)
      grant_resource "Storage Blob Data Contributor" "$STORAGE_ID/blobServices/default/containers/snapshots"
      ;;
    reranker) : ;;  # No Azure role assignments.
  esac
  printf '%s workload identity clientId: %s\n' "$SERVICE" "$CLIENT_ID"
done
# SQL permissions are applied separately by the tracked migration runner.
# DI/Language/classifier-registry and gateway SAS permissions remain absent
# until their held-back implementations are commissioned. RBAC propagation
# and managed-identity access must be verified separately in the target cloud.

# --- demo environment (Container Apps, scales to zero) ------------------
# Optional demo hosting scaffold; review its resource costs and limits
# separately. See deploy/container-apps/demo.yaml.
az containerapp env create -n medw-demo-env -g $RG -l $LOC

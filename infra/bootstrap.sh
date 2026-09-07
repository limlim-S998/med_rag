#!/usr/bin/env bash
# What "using Azure" actually feels like. Read this top to bottom once and the
# portal stops being mysterious - the portal is just a GUI over these calls.
#
# Mental model:
#   subscription > resource group > resource
# A resource group is a folder with a lifecycle. Delete the group, everything
# in it goes. That is why per-environment groups are the default.

set -euo pipefail

LOC=westeurope           # EU data residency was a hard client requirement
ENVN=dev
RG=rg-medw-$ENVN
PREFIX=medw$ENVN

az login
az account set --subscription "$AZ_SUBSCRIPTION_ID"
az group create -n $RG -l $LOC

# --- Azure OpenAI -------------------------------------------------------
# Two levels: the *resource* (an endpoint + quota) and *deployments* inside it
# (named instances of a model). You call the deployment name.
az cognitiveservices account create \
  -n ${PREFIX}aoai -g $RG -l $LOC --kind OpenAI --sku S0 \
  --custom-domain ${PREFIX}aoai

az cognitiveservices account deployment create \
  -n ${PREFIX}aoai -g $RG \
  --deployment-name gpt-4o-2024-08-06 \
  --model-name gpt-4o --model-version 2024-08-06 --model-format OpenAI \
  --sku-name Standard --sku-capacity 60        # capacity = 1000s of TPM

az cognitiveservices account deployment create \
  -n ${PREFIX}aoai -g $RG \
  --deployment-name text-embedding-3-large \
  --model-name text-embedding-3-large --model-version 1 --model-format OpenAI \
  --sku-name Standard --sku-capacity 120

# Note the deployment name carries the version. Name it "gpt-4o" and Azure
# will roll the underlying version forward and your outputs will change with
# no commit anywhere in your repo.

# --- storage ------------------------------------------------------------
# Three containers, three lifecycles. See libs/medw_core/blob.py.
az storage account create -n ${PREFIX}sa -g $RG -l $LOC --sku Standard_LRS
for c in raw parsed snapshots; do
  az storage container create --account-name ${PREFIX}sa -n $c --auth-mode login
done

# --- search -------------------------------------------------------------
az search service create -n ${PREFIX}search -g $RG -l $LOC --sku standard
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
az cosmosdb create -n ${PREFIX}cosmos -g $RG --locations regionName=$LOC \
  --capabilities EnableServerless
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
az sql db create -n medw -s ${PREFIX}sql -g $RG --service-objective S1
az sql server firewall-rule create -n allow-azure -s ${PREFIX}sql -g $RG \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0   # "Azure services", not the world

# --- document parsing + clinical NER ------------------------------------
az cognitiveservices account create -n ${PREFIX}di -g $RG -l $LOC \
  --kind FormRecognizer --sku S0        # Document Intelligence, old CLI name
az cognitiveservices account create -n ${PREFIX}lang -g $RG -l $LOC \
  --kind TextAnalytics --sku S            # Azure AI Language: healthcare NER + UMLS

# --- Azure ML: experiment tracking + the model registry -----------------
# The registry earns its keep on the models that have weights we trained -
# the sklearn classifiers. GPT-4o has no artifact to register, which is why
# its version lives in a Helm value instead.
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
az acr create -n ${PREFIX}acr -g $RG --sku Standard
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

# What Azure actually looks like from inside a service.
#
# The thing worth internalising: you almost never hold a key. You hold a
# *credential object*, and every Azure SDK client takes one. DefaultAzureCredential
# tries a chain of sources and the first that works wins:
#
#   in AKS   -> workload identity. The pod's service account is annotated with a
#               client ID; AKS projects a signed token file into the pod at
#               AZURE_FEDERATED_TOKEN_FILE; the SDK exchanges it for an AAD token.
#               Nothing secret is ever in the cluster.
#   locally  -> whatever `az login` left in your CLI cache.
#   in CI    -> the service connection's federated credential.
#
# Same code in all three. That is the entire point.
#
# --- why the SDK imports are inside the functions -----------------------
#
# This module is shared by every service, so it holds a factory for all six
# Azure clients. Importing a module runs every top-level line in it, so with
# the imports at the top, `from medw_core import azure` required all six SDKs
# to be installed - in every image, including the ones that call two of them.
# The retrieval image does not install azure-cosmos or the Document
# Intelligence SDK and has no reason to, so it could not import this file at
# all.
#
# Moving each import into its factory makes the cost pay-per-use: you need an
# SDK installed only if you actually construct that client. It is the same
# argument as ADR 0005 one layer down - retrieval holds no Cosmos role, so it
# should not be carrying the Cosmos SDK either.
#
# The cost is one import statement per function. Python caches modules in
# sys.modules, so every call after the first is a dict lookup, and these are
# called once per process at startup anyway.

from __future__ import annotations

from typing import TYPE_CHECKING

# azure-identity is the one SDK every service genuinely needs - there is no
# path through this file that does not go through a credential - so it stays
# eager. azure-core comes with it, which is why AzureKeyCredential is safe here.
from azure.core.credentials import AzureKeyCredential  # noqa: F401  (local only)
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

from medw_core.settings import Settings

# Type checkers read this block; the interpreter never executes it. That keeps
# the return annotations honest for mypy in the build pipeline without putting
# the imports back at runtime.
if TYPE_CHECKING:
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.ai.textanalytics.aio import TextAnalyticsClient
    from azure.cosmos.aio import CosmosClient
    from azure.search.documents.aio import SearchClient
    from azure.storage.blob.aio import BlobServiceClient
    from openai import AsyncAzureOpenAI

AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"


def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def openai_client(s: Settings, cred: DefaultAzureCredential) -> AsyncAzureOpenAI:
    # The token provider is a callable the SDK invokes per request; it caches
    # and refreshes internally. No key, no rotation story to own.
    from openai import AsyncAzureOpenAI

    token_provider = get_bearer_token_provider(cred, AOAI_SCOPE)
    return AsyncAzureOpenAI(
        azure_endpoint=s.aoai_endpoint,
        api_version=s.aoai_api_version,
        azure_ad_token_provider=token_provider,
        max_retries=0,   # we do our own backoff; see rate_limit.py
    )


def blob_client(s: Settings, cred: DefaultAzureCredential) -> BlobServiceClient:
    from azure.storage.blob.aio import BlobServiceClient

    return BlobServiceClient(account_url=s.blob_account_url, credential=cred)


def search_client(s: Settings, cred: DefaultAzureCredential) -> SearchClient:
    from azure.search.documents.aio import SearchClient

    return SearchClient(endpoint=s.search_endpoint, index_name=s.search_index, credential=cred)


def cosmos_client(s: Settings, cred: DefaultAzureCredential) -> CosmosClient:
    # Note: AAD auth on Cosmos covers the *data* plane through a separate role
    # family (Cosmos DB Built-in Data Contributor, assigned with
    # `az cosmosdb sql role assignment create` - not `az role assignment`).
    # That distinction is a half-day of confusion the first time you hit it.
    from azure.cosmos.aio import CosmosClient

    return CosmosClient(url=s.cosmos_endpoint, credential=cred)


def docintel_client(s: Settings, cred: DefaultAzureCredential) -> DocumentIntelligenceClient:
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient

    return DocumentIntelligenceClient(endpoint=s.docintel_endpoint, credential=cred)


def language_client(s: Settings, cred: DefaultAzureCredential) -> TextAnalyticsClient:
    from azure.ai.textanalytics.aio import TextAnalyticsClient

    return TextAnalyticsClient(endpoint=s.language_endpoint, credential=cred)


# Azure SQL is the one client that does not take the credential object
# directly - the ODBC driver wants a packed token. See medw_core.sql.


# --- what a call actually looks like ------------------------------------
#
# async with credential() as cred:
#     aoai = openai_client(settings, cred)
#     r = await aoai.embeddings.create(
#         model=settings.embed_deployment,     # <- DEPLOYMENT name, not "text-embedding-3-large"
#         input=["Grade 3 neutropenia by SOC"],
#     )
#     vec = r.data[0].embedding
#
# The `model=` argument being a deployment name is the single most common
# thing people get wrong moving from OpenAI to Azure OpenAI.

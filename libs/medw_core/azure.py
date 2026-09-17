# What Azure actually looks like from inside a service.
#
# The thing worth internalising: you almost never hold a key. You hold a
# *credential object*, and every Azure SDK client takes one. DefaultAzureCredential
# tries a chain of sources and the first that works wins:
#
#   in AKS   -> workload identity. The pod's service account is annotated with a
#               client ID; AKS projects a signed token file into the pod at
#               AZURE_FEDERATED_TOKEN_FILE; the SDK exchanges it for an AAD token.
#               Azure calls need no account key; Qdrant uses separate Secrets.
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
# Retrieval now needs Cosmos for generation selection, but not Document
# Intelligence. It should not require every SDK just to import this module.
#
# Moving each import into its factory makes the cost pay-per-use: you need an
# SDK installed only if you actually construct that client. It is the same
# service-specific dependency boundary described in README.md#major-decisions.
#
# The cost is one import statement per function. Python caches modules in
# sys.modules, so every call after the first is a dict lookup, and these are
# called once per process at startup anyway.

from __future__ import annotations

from typing import TYPE_CHECKING

# The credential factory uses azure-identity and its azure-core dependency.
# The installed placeholder reranker does not construct credentials.
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from pydantic import HttpUrl

from medw_core.settings import Settings, require_setting

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


def _endpoint(value: str | None, name: str) -> str:
    endpoint = require_setting(value, name)
    try:
        url = HttpUrl(endpoint)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid HTTPS URL") from exc
    if url.scheme != "https":
        raise ValueError(f"{name} must use HTTPS")
    return endpoint


def openai_client(s: Settings, cred: DefaultAzureCredential) -> AsyncAzureOpenAI:
    endpoint = _endpoint(s.aoai_endpoint, "MEDW_AOAI_ENDPOINT")
    # The token provider is a callable the SDK invokes per request; it caches
    # and refreshes internally. No key, no rotation story to own.
    from openai import AsyncAzureOpenAI

    token_provider = get_bearer_token_provider(cred, AOAI_SCOPE)
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_version=require_setting(s.aoai_api_version, "MEDW_AOAI_API_VERSION"),
        azure_ad_token_provider=token_provider,
        max_retries=0,   # we do our own backoff; see rate_limit.py
    )


def blob_client(s: Settings, cred: DefaultAzureCredential) -> BlobServiceClient:
    endpoint = _endpoint(s.blob_account_url, "MEDW_BLOB_ACCOUNT_URL")
    from azure.storage.blob.aio import BlobServiceClient

    return BlobServiceClient(account_url=endpoint, credential=cred)


def search_client(s: Settings, cred: DefaultAzureCredential) -> SearchClient:
    endpoint = _endpoint(s.search_endpoint, "MEDW_SEARCH_ENDPOINT")
    from azure.search.documents.aio import SearchClient

    return SearchClient(
        endpoint=endpoint,
        index_name=require_setting(s.search_index, "MEDW_SEARCH_INDEX"),
        credential=cred,
    )


def cosmos_client(s: Settings, cred: DefaultAzureCredential) -> CosmosClient:
    endpoint = _endpoint(s.cosmos_endpoint, "MEDW_COSMOS_ENDPOINT")
    # Note: AAD auth on Cosmos covers the *data* plane through a separate role
    # family (Cosmos DB Built-in Data Contributor, assigned with
    # `az cosmosdb sql role assignment create` - not `az role assignment`).
    # That distinction is a half-day of confusion the first time you hit it.
    from azure.cosmos.aio import CosmosClient

    return CosmosClient(url=endpoint, credential=cred)


def docintel_client(s: Settings, cred: DefaultAzureCredential) -> DocumentIntelligenceClient:
    endpoint = _endpoint(s.docintel_endpoint, "MEDW_DOCINTEL_ENDPOINT")
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient

    return DocumentIntelligenceClient(endpoint=endpoint, credential=cred)


def language_client(s: Settings, cred: DefaultAzureCredential) -> TextAnalyticsClient:
    endpoint = _endpoint(s.language_endpoint, "MEDW_LANGUAGE_ENDPOINT")
    from azure.ai.textanalytics.aio import TextAnalyticsClient

    return TextAnalyticsClient(endpoint=endpoint, credential=cred)


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

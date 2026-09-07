# The single place config enters the process.
# Every field here becomes an env var in the pod, and every env var is set
# from Helm values. That is the whole "three axes" story in one file:
# image = git SHA, model = the deployment/version strings below, deployment =
# the commit on the values file that set them.

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDW_", env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"
    service_name: str = "unset"

    # --- Azure OpenAI ---------------------------------------------------
    # You call a *deployment name*, not a model name. The deployment is a
    # named instance of a model inside your AOAI resource. Pin the version
    # suffix or Azure will roll it forward under you.
    aoai_endpoint: str = "https://med-rag-test1.openai.azure.com/"
    aoai_api_version: str = "2024-10-21"
    chat_deployment: str = "gpt-4.1-mini-2025-04-14"
    embed_deployment: str = "text-embedding-3-large-1"
    embed_dim: int = 3072

    # Bumped whenever the embedding deployment changes. Baked into the
    # Qdrant collection name so old and new vectors can never be compared.
    embed_version: str = "v3l-001"

    # --- Qdrant ---------------------------------------------------------
    qdrant_url: str = "http://localhost:6333"
    hnsw_m: int = 16
    hnsw_ef_construct: int = 128
    search_ef: int = 128

    # --- Azure Cognitive Search (BM25 half) -----------------------------
    search_endpoint: str = "https://medw-dev-search.search.windows.net"
    search_index: str = "csr-chunks"

    # --- Storage / state -------------------------------------------------
    blob_account_url: str = "https://medwdevsa.blob.core.windows.net"
    blob_container: str = "raw"

    # Cosmos: semi-structured, high-churn (documents, jobs, sessions).
    cosmos_endpoint: str = "https://medwdevcosmos.documents.azure.com:443/"
    cosmos_database: str = "medw"

    # Azure SQL: relational + append-only audit. No password field, on purpose:
    # auth is an AAD token, see medw_core.sql.
    sql_server: str = "medwdevsql.database.windows.net"
    sql_database: str = "medw"

    # --- Document parsing / clinical NER ----------------------------------
    docintel_endpoint: str = "https://medwdevdi.cognitiveservices.azure.com/"
    docintel_model: str = "prebuilt-layout"   # layout, not prebuilt-document
    language_endpoint: str = "https://medwdevlang.cognitiveservices.azure.com/"

    # --- Models with weights ----------------------------------------------
    # Registry name + pinned version. Never "latest": a classifier that changes
    # under you changes which table-to-text template fires, which changes the
    # prose, with no commit anywhere.
    table_classifier_name: str = "table-type-classifier"
    table_classifier_version: str = "7"
    table_classifier_min_proba: float = 0.65   # below this, generic template
    azureml_workspace: str = "medw-dev-ws"

    # --- Telemetry ---------------------------------------------------------
    appinsights_connection_string: str = ""    # empty = console logging only

    # --- Retrieval knobs --------------------------------------------------
    rrf_k: int = 60
    fusion_top_n: int = 30      # what goes into the cross-encoder
    rerank_top_k: int = 8       # what comes out, into the generator
    reranker_url: str = "http://reranker:8000"

    # --- Prompts ----------------------------------------------------------
    # Hash of the prompt directory. Logged on every generation so you can
    # answer "which prompt produced this paragraph" six months later.
    prompt_bundle_sha: str = "local-dev"

    # In-cluster this is unset and DefaultAzureCredential uses workload
    # identity. Locally it is unset too and you fall back to `az login`.
    # There is deliberately no api_key field. Adding one is how keys end up
    # in a values file.


@lru_cache
def get_settings() -> Settings:
    return Settings()

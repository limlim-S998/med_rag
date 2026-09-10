# The single place config enters the process.
# MEDW_ environment variables supply values; Helm supplies deployed settings.
# Source revision, image digest, model pins, prompt content and release/config
# identity are distinct. See README.md#releases-and-versioning.

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDW_", env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"
    service_name: str = "unset"

    # --- which implementations get wired -----------------------------------
    # Composition chooses dependency implementations. Platform startup also
    # uses the backend to validate identity and synthetic-mode prerequisites.
    #
    #   azure  real services, real credential, real cost
    #   local  persistent SQLite/artifact stores and synthetic AI adapters
    backend: Literal["local", "azure"] = "azure"

    # Local persistent stores use this file across process restarts. Tests can
    # supply a temporary path; production never selects the local backend.
    local_state_path: str = "/tmp/medw-platform.sqlite3"
    local_artifact_dir: str = "/tmp/medw-artifacts"
    synthetic_enabled: bool = False
    readiness_timeout: float = Field(default=3.0, gt=0, le=30)
    readiness_cache_seconds: float = Field(default=5.0, ge=0, le=60)

    # Recorded Document Intelligence layout responses, replayed by the local
    # LayoutExtractor. Point this at a directory of *.layout.json.
    fixture_dir: str = "data/sample/ABC-101"

    # --- Azure OpenAI ---------------------------------------------------
    # You call a *deployment name*, not a model name. The deployment is a
    # named instance of a model inside your AOAI resource. Pin the version
    # metadata check validates the actual model version and NoAutoUpgrade;
    # a name suffix alone does not pin an Azure deployment.
    aoai_endpoint: str = ""
    aoai_api_version: str = "2024-10-21"
    aoai_resource_id: str = ""
    chat_deployment: str = "gpt-4.1-mini-2025-04-14"
    embed_deployment: str = "text-embedding-3-large-1"
    embed_dim: int = 3072
    chat_model_name: str = "gpt-4.1-mini"
    chat_model_version: str = "2025-04-14"
    embed_model_name: str = "text-embedding-3-large"
    embed_model_version: str = "1"

    # Compatibility label carried by each generation, alongside actual model
    # identity and dimensions. Readers reject a mismatched selected generation.
    embed_version: str = "v3l-001"

    # This pod's share of the deployment's tokens-per-minute quota. Per-pod,
    # not global: a distributed limiter would need a shared store on the hot
    # path to solve what replica-count arithmetic already solves. maxReplicas
    # in values.yaml times this number must stay under the provisioned quota.
    pod_tpm: int = 10_000
    embed_pod_tpm: int = 10_000

    # --- Qdrant ---------------------------------------------------------
    qdrant_url: str = "http://localhost:6333"
    # Injected from a Kubernetes Secret. Retrieval receives only the read key.
    qdrant_api_key: str = Field(default="", repr=False)
    qdrant_replication_factor: int = Field(default=1, ge=1)
    qdrant_shard_number: int = Field(default=1, ge=1)
    qdrant_write_consistency_factor: int = Field(default=1, ge=1)
    hnsw_m: int = 16
    hnsw_ef_construct: int = 128
    search_ef: int = 128

    # --- Azure Cognitive Search (BM25 half) -----------------------------
    search_endpoint: str = ""
    search_index: str = "csr-chunks"

    # --- Storage / state -------------------------------------------------
    blob_account_url: str = ""
    blob_container: str = "raw"

    # Cosmos: semi-structured, high-churn (documents, jobs, sessions).
    cosmos_endpoint: str = ""
    cosmos_database: str = "medw"
    cosmos_state_container: str = "platform-state"

    # Azure SQL: relational + append-only audit. No password field, on purpose:
    # auth is an AAD token, see medw_core.sql.
    sql_server: str = ""   # not provisioned yet
    sql_database: str = "medw"

    # --- Document parsing / clinical NER ----------------------------------
    # NOTE the random suffix. Azure generates a custom subdomain when one is
    # not requested, so this URL CANNOT be built from the resource name -
    # it has to be read back with `az cognitiveservices account show`.
    docintel_endpoint: str = ""
    docintel_model: str = "prebuilt-layout"   # layout, not prebuilt-document
    language_endpoint: str = ""

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
    retrieval_url: str = "http://retrieval:8000"
    generation_url: str = "http://generation:8000"
    ingestion_url: str = "http://ingestion-worker:8000"

    # Fixed by deployment configuration, never derived from an unverified JWT.
    auth_tenant_id: str = ""
    auth_audience: str = ""
    auth_issuer: str = ""
    auth_jwks_url: str = ""
    auth_jwks_cache_seconds: float = Field(default=300, gt=0, le=3600)

    # --- Prompts ----------------------------------------------------------
    # Hash of the prompt directory. Logged on every generation so you can
    # answer "which prompt produced this paragraph" six months later.
    prompt_bundle_sha: str = "local-dev"

    # Helm supplies source attribution separately from the selected digest.
    # Azure readiness compares image_sha with the source baked into the image;
    # deployment_revision hashes effective values, not a claimed Git revision.
    image_sha: str = "unknown"
    build_source_sha: str = "unversioned"
    image_digest: str = ""
    release_bundle_sha: str = ""
    deployment_revision: str = ""

    @model_validator(mode="after")
    def validate_modes(self):
        if self.backend == "local" and self.env == "prod":
            raise ValueError("the local backend cannot run in prod")
        if self.synthetic_enabled and (self.backend != "local" or self.env not in {"local", "test"}):
            raise ValueError("synthetic work requires backend=local and env=local or test")
        if self.qdrant_write_consistency_factor > self.qdrant_replication_factor:
            raise ValueError("Qdrant write consistency cannot exceed replication")
        if self.auth_jwks_url and self.backend == "azure" and not self.auth_jwks_url.startswith("https://"):
            raise ValueError("Azure JWKS URL must use HTTPS")
        return self

    # In-cluster this is unset and DefaultAzureCredential uses workload
    # identity. Explicit host-side Azure development can use `az login`.
    # Azure services use identity rather than account keys. Qdrant is the
    # explicit key-based exception, injected from a Kubernetes Secret.


@lru_cache
def get_settings() -> Settings:
    return Settings()

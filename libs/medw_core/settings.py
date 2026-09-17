# The single place config enters the process.
# MEDW_ environment variables supply values; Helm supplies deployed settings.
# Source revision, image digest, model pins, prompt content and release/config
# identity are distinct. See README.md#releases-and-versioning.

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration shared by the application processes.

    Every service can access these fields. Optional Azure values are checked
    where they are used, so each service needs only its own Azure dependencies.
    """

    model_config = SettingsConfigDict(
        env_prefix="MEDW_", env_file=".env", extra="ignore"
    )

    env: str = "dev"
    log_level: str = "INFO"
    service_name: str = "unset"

    readiness_timeout: float = Field(default=3.0, gt=0, le=30)
    readiness_cache_seconds: float = Field(default=5.0, ge=0, le=60)
    parser_version: str = "placeholder-text-1"
    upload_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    upload_ttl_seconds: int = Field(default=900, ge=1, le=3600)
    ingestion_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    ingestion_lease_seconds: float = Field(default=60, ge=5, le=3600)
    ingestion_max_attempts: int = Field(default=5, ge=1, le=20)

    # --- Installed model identities and dormant Azure model adapters -----
    # Current identities name the packaged placeholders. Azure model adapters
    # remain available for later integration; they use endpoint/resource inputs
    # below and require remote deployment metadata checks when actually wired.
    aoai_endpoint: str | None = None
    aoai_api_version: str = "2024-10-21"
    aoai_resource_id: str | None = None
    chat_deployment: str = "scripted-chat"
    embed_deployment: str = "hash-1"
    embed_dim: int = 64
    chat_model_name: str = "scripted-placeholder"
    chat_model_version: str = "1"
    embed_model_name: str = "token-hash"
    embed_model_version: str = "1"

    # Compatibility label carried by each generation, alongside actual model
    # identity and dimensions. Readers reject a mismatched selected generation.
    embed_version: str = "hash-1"

    # Retained Azure adapters: this pod's share of tokens-per-minute quota. Per-pod,
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
    search_endpoint: str | None = None
    search_index: str = "csr-chunks"

    # --- Storage / state -------------------------------------------------
    blob_account_url: str | None = None
    blob_container: str = "raw"

    # Cosmos: semi-structured, high-churn (documents, jobs, sessions).
    cosmos_endpoint: str | None = None
    cosmos_database: str = "medw"
    cosmos_state_container: str = "platform-state"

    # Azure SQL: relational + append-only audit. No password field, on purpose:
    # auth is an AAD token, see medw_core.sql.
    sql_server: str | None = None
    sql_database: str = "medw"

    # --- Document parsing / clinical NER ----------------------------------
    # NOTE the random suffix. Azure generates a custom subdomain when one is
    # not requested, so this URL CANNOT be built from the resource name -
    # it has to be read back with `az cognitiveservices account show`.
    docintel_endpoint: str | None = None
    docintel_model: str = "prebuilt-layout"  # layout, not prebuilt-document
    language_endpoint: str | None = None

    # --- Models with weights ----------------------------------------------
    # Registry name + pinned version. Never "latest": a classifier that changes
    # under you changes which table-to-text template fires, which changes the
    # prose, with no commit anywhere.
    table_classifier_name: str = "placeholder-table-classifier"
    table_classifier_version: str = "placeholder-1"
    table_classifier_min_proba: float = 0.65  # below this, generic template
    azureml_workspace: str | None = None

    # --- Telemetry ---------------------------------------------------------
    # Absent: console logs, Prometheus and trace context still work; no Azure export.
    appinsights_connection_string: str | None = Field(default=None, repr=False)

    # --- Retrieval knobs --------------------------------------------------
    rrf_k: int = 60
    fusion_top_n: int = 30  # what goes into the cross-encoder
    rerank_top_k: int = 8  # what comes out, into the generator
    reranker_url: str = "http://reranker:8000"
    retrieval_url: str = "http://retrieval:8000"

    # Fixed by deployment configuration, never derived from an unverified JWT.
    auth_tenant_id: str = ""
    auth_audience: str = ""
    auth_issuer: str = ""
    auth_jwks_url: str | None = None
    auth_jwks_cache_seconds: float = Field(default=300, gt=0, le=3600)

    # --- Prompts ----------------------------------------------------------
    # Hash of the prompt directory. Logged on every generation so you can
    # answer "which prompt produced this paragraph" six months later.
    prompt_bundle_sha: str = "unversioned"

    # Helm supplies source attribution separately from the selected digest.
    # Azure readiness compares image_sha with the source baked into the image;
    # deployment_revision hashes effective values, not a claimed Git revision.
    image_sha: str = "unknown"
    build_source_sha: str = "unversioned"
    image_digest: str = ""
    release_bundle_sha: str = ""
    deployment_revision: str = ""

    @field_validator(
        "aoai_endpoint", "aoai_resource_id", "search_endpoint", "blob_account_url",
        "cosmos_endpoint", "sql_server", "docintel_endpoint", "language_endpoint",
        "appinsights_connection_string", "auth_jwks_url", mode="before",
    )
    @classmethod
    def blank_is_absent(cls, value):
        # Existing Helm values and .env files use empty strings for unset values.
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.qdrant_write_consistency_factor > self.qdrant_replication_factor:
            raise ValueError("Qdrant write consistency cannot exceed replication")
        if (
            self.auth_jwks_url
            and not self.auth_jwks_url.startswith("https://")
        ):
            raise ValueError("Azure JWKS URL must use HTTPS")
        return self

    # In-cluster this is unset and DefaultAzureCredential uses workload
    # identity. Explicit host-side Azure development can use `az login`.
    # Azure services use identity rather than account keys. Qdrant is the
    # explicit key-based exception, injected from a Kubernetes Secret.


def require_setting(value: str | None, name: str) -> str:
    """Check a required string at its point of use and name its environment variable."""
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    return value.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()

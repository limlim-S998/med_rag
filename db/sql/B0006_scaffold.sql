-- Flyway baseline for a fresh database at schema version 6.
-- Historical numbered migrations remain immutable for legacy verification.
-- This cumulative schema omits environment-specific users and permissions;
-- migration 7 establishes roles, then sql_admin.py binds real identities.

GO
-- Source schema: 0001_core.sql
-- The relational registry. Small, slow-changing, heavily joined.
--
-- Everything here has a natural key that came from the client (study_id is
-- the sponsor's protocol number, not ours). Surrogate keys would add a join
-- and buy nothing: these identifiers are stable because a regulator uses them.

CREATE SCHEMA core;
GO

CREATE TABLE core.study (
    study_id        VARCHAR(32)   NOT NULL PRIMARY KEY,   -- 'ABC-101'
    sponsor         NVARCHAR(200) NOT NULL,
    indication      NVARCHAR(400) NULL,
    phase           VARCHAR(8)    NULL,
    -- Data residency is a per-study fact, not a per-tenant one. A study whose
    -- data may not leave the EU pins the whole pipeline to a region.
    data_region     VARCHAR(32)   NOT NULL DEFAULT 'westeurope',
    teardown_at     DATETIME2     NULL,   -- set on client offboarding; the
                                          -- teardown job reads this and drops
                                          -- the blob prefix + Qdrant collection
    created_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE core.document (
    doc_id          VARCHAR(64)   NOT NULL PRIMARY KEY,
    study_id        VARCHAR(32)   NOT NULL REFERENCES core.study(study_id),
    doc_type        VARCHAR(16)   NOT NULL,   -- protocol | tfl | prior_csr | sap
    -- doc_type is written by the sklearn classifier, so the confidence and the
    -- model version that produced it are stored with it. A label with no
    -- provenance is not auditable, and this label decides which parser runs.
    doc_type_proba  FLOAT         NULL,
    classifier_ver  VARCHAR(16)   NULL,
    blob_path       NVARCHAR(512) NOT NULL,
    parser_version  VARCHAR(8)    NOT NULL,
    page_count      INT           NULL,
    ingested_at     DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_doc_type CHECK
        (doc_type IN ('protocol', 'tfl', 'prior_csr', 'sap'))
);

CREATE INDEX ix_document_study ON core.document(study_id, doc_type);

-- The ICH E3 shell. Rows are seeded from the standard, not user-created:
-- "which sections must exist in a CSR" is a fixed regulatory fact, and
-- verify.py's structural layer checks a draft against these rows.
CREATE TABLE core.e3_section (
    section_path    VARCHAR(32)   NOT NULL PRIMARY KEY,   -- '11.4.2'
    title           NVARCHAR(300) NOT NULL,
    required        BIT           NOT NULL DEFAULT 1,
    parent_path     VARCHAR(32)   NULL REFERENCES core.e3_section(section_path)
);

-- A writer's draft of one section of one study. The current state only;
-- the history of how it got here is in audit.generation_event.
CREATE TABLE core.section_draft (
    study_id        VARCHAR(32)   NOT NULL REFERENCES core.study(study_id),
    section_path    VARCHAR(32)   NOT NULL REFERENCES core.e3_section(section_path),
    status          VARCHAR(16)   NOT NULL DEFAULT 'draft',  -- draft|in_review|accepted
    accepted_by_oid VARCHAR(64)   NULL,   -- the writer is the accountable author
    updated_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    PRIMARY KEY (study_id, section_path)
);

GO
-- Source schema: 0002_audit.sql
-- The audit trail. The one table in this system a regulator might read.
--
-- Append-only, enforced by grant (0003_grants.sql) rather than by convention.
-- The row is the answer to "why does this paragraph say what it says": who
-- asked, which model version and prompt bundle produced it, which source
-- chunks it drew on, and whether every verification layer passed.
--
-- Scope note, because it matters in an interview: this is OUR audit trail, not
-- the regulated system of record. The client's DMS is 21 CFR Part 11 validated
-- and holds the signed submission. We are a drafting tool whose output a human
-- author accepts. Claiming Part 11 compliance for this service would be a
-- claim we could not support; being able to say precisely where the boundary
-- sits is the point.

CREATE SCHEMA audit;
GO

CREATE TABLE audit.generation_event (
    event_id            UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    -- One writer action = one correlation ID, minted at the gateway and
    -- threaded through retrieval, reranking and generation. Same ID that
    -- stitches the App Insights trace, so a row here joins to a distributed
    -- trace without any extra plumbing.
    correlation_id      CHAR(32)      NOT NULL,
    study_id            VARCHAR(32)   NOT NULL REFERENCES core.study(study_id),
    section_path        VARCHAR(32)   NOT NULL,
    user_oid            VARCHAR(64)   NOT NULL,   -- AAD object ID, never email

    -- The three version axes, denormalised on purpose. If these were foreign
    -- keys to a config table, editing that table would rewrite history.
    chat_deployment     VARCHAR(64)   NOT NULL,   -- 'gpt-4o-2024-08-06'
    prompt_bundle_sha   VARCHAR(40)   NOT NULL,
    embed_version       VARCHAR(16)   NOT NULL,
    image_sha           VARCHAR(40)   NULL,

    -- Provenance: the chunk IDs the paragraph was built from. JSON array, and
    -- SQL Server can index into it if the query ever needs to.
    source_chunk_ids    NVARCHAR(MAX) NOT NULL,
    source_table_number VARCHAR(32)   NULL,       -- 'Table 14.3.2.1'

    -- Verification outcomes, one column per layer. Split, not a single
    -- boolean, because the interesting alerting question is *which* layer
    -- started failing after a deployment.
    numeric_check_passed    BIT NOT NULL,
    structural_check_passed BIT NOT NULL,
    judgement_verdict       NVARCHAR(MAX) NULL,   -- the StructuralVerdict JSON
    retry_count             INT NOT NULL DEFAULT 0,

    prompt_tokens       INT NULL,
    completion_tokens   INT NULL,
    latency_ms          INT NULL,
    created_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT ck_json_chunks CHECK (ISJSON(source_chunk_ids) = 1)
);

-- The two queries this table exists to answer.
--   1. "Show me the provenance of this section."       -> study + section
--   2. "Everything drafted on a superseded model."     -> deployment + date
CREATE INDEX ix_gen_study_section ON audit.generation_event(study_id, section_path, created_at DESC);
CREATE INDEX ix_gen_version       ON audit.generation_event(chat_deployment, prompt_bundle_sha, created_at DESC);

-- Ingestion side: what was indexed, when, by which parser. This is how you
-- answer "which studies still need a backfill" after a parser fix, without
-- scanning Qdrant.
CREATE TABLE audit.index_event (
    event_id        UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    study_id        VARCHAR(32)   NOT NULL REFERENCES core.study(study_id),
    doc_id          VARCHAR(64)   NOT NULL,
    parser_version  VARCHAR(8)    NOT NULL,
    embed_version   VARCHAR(16)   NOT NULL,
    collection      NVARCHAR(128) NOT NULL,
    chunks_upserted INT           NOT NULL,
    dag_run_id      NVARCHAR(200) NULL,
    created_at      DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
);

GO
-- Source schema: 0004_durable_contracts.sql
-- Additive migration: historical rows remain readable; new writes require
-- complete provenance through the adapter contract. Identity/access claims
-- are facts of the selected request, never inferred from current configuration.
ALTER TABLE audit.generation_event ALTER COLUMN prompt_bundle_sha VARCHAR(80) NOT NULL;
GO
ALTER TABLE audit.generation_event ADD
    env VARCHAR(32) NULL,
    service VARCHAR(64) NULL,
    image_digest VARCHAR(80) NULL,
    release_bundle_sha VARCHAR(80) NULL,
    deployment_revision VARCHAR(96) NULL,
    chat_model_name VARCHAR(128) NULL,
    chat_model_version VARCHAR(64) NULL,
    embed_model_name VARCHAR(128) NULL,
    embed_model_version VARCHAR(64) NULL,
    classifier_version VARCHAR(64) NULL,
    index_generation_id VARCHAR(64) NULL,
    index_manifest NVARCHAR(MAX) NULL,
    source_citations NVARCHAR(MAX) NULL,
    output_text NVARCHAR(MAX) NULL,
    output_sha256 CHAR(64) NULL;
GO
ALTER TABLE audit.generation_event ADD
    CONSTRAINT ck_audit_index_json CHECK (index_manifest IS NULL OR ISJSON(index_manifest)=1),
    CONSTRAINT ck_audit_citations_json CHECK (source_citations IS NULL OR ISJSON(source_citations)=1);
GO
ALTER TABLE audit.index_event ADD
    index_generation_id VARCHAR(64) NULL,
    source_revision VARCHAR(64) NULL;
GO
CREATE TABLE core.study_access (
    study_id VARCHAR(32) NOT NULL REFERENCES core.study(study_id),
    user_oid VARCHAR(64) NOT NULL,
    granted_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    revoked_at DATETIME2 NULL,
    PRIMARY KEY (study_id, user_oid)
);
GO
-- Only a separate administration identity changes membership. Gateway reads it.

GO
-- Source schema: 0005_application_workflow.sql
-- One draft reference per immutable generation; no update to historical audit.
CREATE TABLE core.generated_draft (
    draft_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    event_id UNIQUEIDENTIFIER NOT NULL UNIQUE REFERENCES audit.generation_event(event_id),
    study_id VARCHAR(32) NOT NULL REFERENCES core.study(study_id),
    section_path VARCHAR(32) NOT NULL,
    created_by_oid VARCHAR(64) NOT NULL,
    output_sha256 CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',
    accepted_by_oid VARCHAR(64) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    accepted_at DATETIME2 NULL,
    CONSTRAINT ck_generated_draft_status CHECK (status IN ('draft','accepted'))
);
CREATE INDEX ix_generated_draft_section ON core.generated_draft(study_id,section_path);
CREATE TABLE audit.acceptance_event (
    draft_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY REFERENCES core.generated_draft(draft_id),
    user_oid VARCHAR(64) NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
CREATE PROCEDURE core.accept_generated_draft
    @study_id VARCHAR(32), @section_path VARCHAR(32),
    @draft_id UNIQUEIDENTIFIER, @user_oid VARCHAR(64), @correlation_id VARCHAR(128)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;
    DECLARE @status VARCHAR(16), @accepted_by VARCHAR(64);
    SELECT @status=status,@accepted_by=accepted_by_oid
        FROM core.generated_draft WITH (UPDLOCK,HOLDLOCK)
        WHERE draft_id=@draft_id AND study_id=@study_id AND section_path=@section_path;
    IF @status IS NULL
    BEGIN
        COMMIT; SELECT 'missing'; RETURN;
    END;
    IF @status='accepted' AND @accepted_by<>@user_oid
    BEGIN
        COMMIT; SELECT 'conflict'; RETURN;
    END;
    IF @status='draft'
    BEGIN
        UPDATE core.generated_draft SET status='accepted',accepted_by_oid=@user_oid,
            accepted_at=SYSUTCDATETIME() WHERE draft_id=@draft_id;
        INSERT INTO audit.acceptance_event(draft_id,user_oid,correlation_id)
            VALUES (@draft_id,@user_oid,@correlation_id);
    END;
    COMMIT;
    SELECT 'accepted';
END;
GO
ALTER TABLE audit.index_event ADD correlation_id VARCHAR(128) NULL;
-- Widen to the existing bounded trace header contract, retaining old values.
ALTER TABLE audit.generation_event ALTER COLUMN correlation_id VARCHAR(128) NOT NULL;
-- Longer placeholder parser identifiers must not be silently truncated.
ALTER TABLE audit.index_event ALTER COLUMN parser_version VARCHAR(64) NOT NULL;
ALTER TABLE core.document ALTER COLUMN parser_version VARCHAR(64) NOT NULL;
GO
-- Generation appends only its own event kind; acceptance is procedure-owned.
GO

GO
-- Source schema: 0006_batch_ingestion.sql
-- Workers now validate writer membership themselves as well as at NGINX.
-- Airflow's workload has no SQL grants; it can only coordinate admitted batches.
ALTER TABLE audit.index_event ADD job_id VARCHAR(64) NULL;
ALTER TABLE audit.index_event ADD batch_id CHAR(64) NULL;
ALTER TABLE audit.index_event ADD requested_by_oid VARCHAR(64) NULL;
GO

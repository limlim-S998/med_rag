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

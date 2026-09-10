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
GRANT SELECT ON core.study_access TO [id-medw-gateway];
DENY UPDATE, DELETE ON SCHEMA::audit TO [id-medw-generation];
DENY UPDATE, DELETE ON SCHEMA::audit TO [id-medw-ingestion];

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

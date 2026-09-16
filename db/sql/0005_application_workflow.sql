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
GRANT SELECT, INSERT ON core.generated_draft TO [id-medw-generation];
GRANT SELECT ON core.generated_draft TO [id-medw-gateway];
GRANT EXECUTE ON core.accept_generated_draft TO [id-medw-gateway];
GRANT SELECT ON audit.index_event TO [id-medw-ingestion];
-- Generation appends only its own event kind; acceptance is procedure-owned.
REVOKE INSERT ON SCHEMA::audit FROM [id-medw-generation];
GRANT INSERT ON audit.generation_event TO [id-medw-generation];
GO

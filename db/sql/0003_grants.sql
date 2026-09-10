-- Contained AAD users, one per workload identity. No SQL logins, no password
-- anywhere: the managed identity IS the database principal, created FROM
-- EXTERNAL PROVIDER by its name in AAD.
--
-- This file is the whole append-only guarantee. An audit table you have only
-- promised not to modify is a policy; one the service cannot modify is a
-- property. The difference is the two lines that are absent below.

-- SQL Server local verification uses contained users without logins. Azure
-- retains the workload-identity principals; no SQL password is introduced.
IF CAST(SERVERPROPERTY('EngineEdition') AS INT) = 5
BEGIN
    EXEC('CREATE USER [id-medw-generation] FROM EXTERNAL PROVIDER');
    EXEC('CREATE USER [id-medw-ingestion] FROM EXTERNAL PROVIDER');
    EXEC('CREATE USER [id-medw-gateway] FROM EXTERNAL PROVIDER');
END
ELSE
BEGIN
    CREATE USER [id-medw-generation] WITHOUT LOGIN;
    CREATE USER [id-medw-ingestion] WITHOUT LOGIN;
    CREATE USER [id-medw-gateway] WITHOUT LOGIN;
END;
GO

-- Generation: writes the audit trail, reads the E3 shell for structural rules.
GRANT INSERT ON SCHEMA::audit TO [id-medw-generation];
GRANT SELECT ON SCHEMA::core  TO [id-medw-generation];
-- Deliberately NOT granted: UPDATE, DELETE on audit.
-- Retention is a scheduled job under a separate principal, not this one.

-- Ingestion: owns the document registry, appends index events.
GRANT SELECT, INSERT, UPDATE ON core.document TO [id-medw-ingestion];
GRANT INSERT ON audit.index_event TO [id-medw-ingestion];

-- Gateway: reads registry + draft state, updates draft status on accept.
GRANT SELECT ON SCHEMA::core TO [id-medw-gateway];
GRANT UPDATE ON core.section_draft TO [id-medw-gateway];

-- Nobody has db_owner at runtime. Migrations run in the pipeline under a
-- separate service connection identity that exists only for that stage.

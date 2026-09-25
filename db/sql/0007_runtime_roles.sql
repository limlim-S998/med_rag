-- Permissions belong to stable database roles. An explicit administrator maps
-- each environment's managed-identity client ID to its contained user/role.
-- No Graph directory lookup, fixed Azure identity name, or runtime db_owner.
IF DATABASE_PRINCIPAL_ID('medw_generation') IS NULL
    CREATE ROLE medw_generation AUTHORIZATION dbo;
IF DATABASE_PRINCIPAL_ID('medw_ingestion') IS NULL
    CREATE ROLE medw_ingestion AUTHORIZATION dbo;
IF DATABASE_PRINCIPAL_ID('medw_gateway') IS NULL
    CREATE ROLE medw_gateway AUTHORIZATION dbo;
GO
GRANT SELECT ON SCHEMA::core TO medw_generation;
GRANT INSERT ON audit.generation_event TO medw_generation;
GRANT INSERT ON core.generated_draft TO medw_generation;
DENY UPDATE, DELETE ON SCHEMA::audit TO medw_generation;

GRANT SELECT, INSERT, UPDATE ON core.document TO medw_ingestion;
GRANT SELECT, INSERT ON audit.index_event TO medw_ingestion;
GRANT SELECT ON core.study_access TO medw_ingestion;
DENY UPDATE, DELETE ON SCHEMA::audit TO medw_ingestion;

GRANT SELECT ON SCHEMA::core TO medw_gateway;
GRANT UPDATE ON core.section_draft TO medw_gateway;
GRANT EXECUTE ON core.accept_generated_draft TO medw_gateway;
GO

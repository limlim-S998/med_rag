"""Opt-in real Flyway/SQL test, using isolated containers and no Azure resources.

MEDW_SQL_IMAGE_TESTS=1 pytest tests/integration/test_sql_migrations.py -q
Build medw-generation:scaffold-local first, or set MEDW_SQL_TEST_IMAGE to the
generation image built by CI. No ports are published on the host.
"""
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
FLYWAY = "redgate/flyway:13.7.0@sha256:031f7127435cdfcf3aa477b15c88fdd4a42145395d314914426f28dc65c10bfd"
SQL_SERVER = "mcr.microsoft.com/mssql/server:2022-CU17-ubuntu-22.04"


def inside_container(stage):
    import pyodbc

    from scripts.sql_admin import verify_legacy

    target = os.environ["MEDW_TEST_SQL_SERVER"]
    if not target.startswith("medw-sql-test-"):
        raise ValueError("This check only accepts its disposable container name")
    dsn = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={target};UID=sa;"
           f"PWD={os.environ['MSSQL_SA_PASSWORD']};Encrypt=yes;TrustServerCertificate=yes;DATABASE=")
    for attempt in range(60):
        try:
            master = pyodbc.connect(dsn + "master", autocommit=True, timeout=1)
            break
        except pyodbc.Error:
            if attempt == 59:
                raise
            time.sleep(1)
    assert master.execute("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)").fetchone()[0] != 5
    if stage == "prepare":
        for database in ("fresh", "legacy"):
            master.execute(f"CREATE DATABASE [{database}]")
        with pyodbc.connect(dsn + "legacy") as connection:
            connection.execute("CREATE TABLE dbo.SchemaMigrations(name nvarchar(255) PRIMARY KEY, sha256 char(64))")
            # Fixture construction only: reproduces the already deployed history
            # independently of Flyway's new baseline. Not a migration runner.
            for path in sorted((ROOT / "db/sql").glob("000[1-6]_*.sql")):
                for batch in re.split(r"^\s*GO\s*(?:--[^\n]*)?$", path.read_text(), flags=re.MULTILINE | re.IGNORECASE):
                    if batch.strip():
                        connection.execute(batch)
                connection.execute("INSERT dbo.SchemaMigrations VALUES(?,?)", path.name,
                                   hashlib.sha256(path.read_bytes()).hexdigest())
            connection.commit()
            assert verify_legacy(connection.cursor())["baseline_version"] == 6
            connection.execute("UPDATE dbo.SchemaMigrations SET sha256=? WHERE name='0001_core.sql'", "0" * 64)
            try:
                verify_legacy(connection.cursor())
            except ValueError:
                pass
            else:
                raise AssertionError("Changed legacy ledger accepted")
            connection.rollback()
    else:
        fresh, legacy = pyodbc.connect(dsn + "fresh"), pyodbc.connect(dsn + "legacy")
        query = ("SELECT s.name,t.name,c.name,c.column_id,c.system_type_id,c.max_length,c.is_nullable,"
                 "c.precision,c.scale FROM sys.columns c JOIN sys.tables t ON c.object_id=t.object_id "
                 "JOIN sys.schemas s ON t.schema_id=s.schema_id WHERE s.name IN ('core','audit') "
                 "ORDER BY s.name,t.name,c.column_id")
        assert list(fresh.execute(query).fetchall()) == list(legacy.execute(query).fetchall())
        for user, role, table in (("writer", "medw_generation", "audit.generation_event"),
                                  ("indexer", "medw_ingestion", "audit.index_event")):
            fresh.execute(f"CREATE USER [{user}] WITHOUT LOGIN")
            fresh.execute(f"ALTER ROLE [{role}] ADD MEMBER [{user}]")
            fresh.execute(f"EXECUTE AS USER='{user}'")
            permissions = fresh.execute(
                "SELECT HAS_PERMS_BY_NAME(?,'OBJECT','INSERT'), HAS_PERMS_BY_NAME(?,'OBJECT','UPDATE'),"
                "HAS_PERMS_BY_NAME(?,'OBJECT','DELETE')", table, table, table).fetchone()
            assert tuple(permissions) == (1, 0, 0)
            fresh.execute("REVERT")
        fresh.close()
        legacy.close()
        print(json.dumps({"schema_equivalence": True, "runtime_audit_append_only": True}))
    master.close()


def test_flyway_fresh_legacy_repeat_and_runtime_grants():
    import pytest

    if os.getenv("MEDW_SQL_IMAGE_TESTS") != "1":
        pytest.skip("Set MEDW_SQL_IMAGE_TESTS=1 to run the isolated container check")
    name = "medw-sql-test-" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32) + "aA1!"
    environment = {**os.environ, "MSSQL_SA_PASSWORD": password, "FLYWAY_PASSWORD": password,
                   "MEDW_TEST_SQL_SERVER": name}
    generation_image = os.getenv("MEDW_SQL_TEST_IMAGE", "medw-generation:scaffold-local")

    def run(*args, check=True):
        result = subprocess.run(args, check=False, env=environment, capture_output=True, text=True)
        if check and result.returncode:
            diagnostic = (result.stdout + "\n" + result.stderr).replace(password, "[redacted]")
            pytest.fail(f"Container command failed ({result.returncode}): {' '.join(args[:3])}\n"
                        + diagnostic[-6000:], pytrace=False)
        return result

    def fixture(stage):
        return run("docker", "run", "--rm", "--network", name, "--env", "MSSQL_SA_PASSWORD",
                   "--env", "MEDW_TEST_SQL_SERVER", "--env", "PYTHONPATH=/workspace:/workspace/tests",
                   "--mount", f"type=bind,src={ROOT},dst=/workspace,readonly",
                   "--workdir", "/workspace", generation_image, "python", "-m",
                   "integration.test_sql_migrations", stage)

    def flyway(database, *args):
        return run("docker", "run", "--rm", "--network", name, "--env", "FLYWAY_PASSWORD",
                   "--env", "FLYWAY_USER=sa", "--mount", f"type=bind,src={ROOT / 'db'},dst=/schema,readonly",
                   FLYWAY, "-configFiles=/schema/flyway.conf", "-locations=filesystem:/schema/sql",
                   f"-url=jdbc:sqlserver://{name}:1433;databaseName={database};encrypt=true;trustServerCertificate=true",
                   *args)

    run("docker", "network", "create", name)
    try:
        run("docker", "run", "-d", "--name", name, "--network", name, "--env", "MSSQL_SA_PASSWORD",
            "--env", "ACCEPT_EULA=Y", "--env", "MSSQL_PID=Developer", SQL_SERVER)
        fixture("prepare")
        flyway("fresh", "migrate")
        repeated = flyway("fresh", "validate", "migrate")
        assert "No migration necessary" in repeated.stdout
        flyway("legacy", "-baselineVersion=6", "baseline", "migrate")
        fixture("verify")
    finally:
        run("docker", "rm", "-f", name, check=False)
        run("docker", "network", "rm", name, check=False)


if __name__ == "__main__":
    import sys
    inside_container(sys.argv[1])

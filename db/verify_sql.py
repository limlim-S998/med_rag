"""Opt-in destructive verification, ONLY against a disposable SQL Server instance.

Uses MSSQL_SA_PASSWORD and MEDW_TEST_SQL_SERVER (default medw-sql-proof).
Creates a randomly named test database and never connects to a supplied application DB.
Run in the generation image, which contains the Microsoft ODBC driver.
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import pyodbc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from medw_core.audit_events import AUDIT_COLUMNS, generation_event, normalize_generation
from medw_core.indexing import make_generation
from medw_core.provenance import Provenance
from medw_core.schemas import Chunk, Citation, DocType
from medw_core.sql import SqlAuditSink, SqlStudyAccess
from scripts.migrate import apply


async def main():
    if os.environ.get("MEDW_ALLOW_DISPOSABLE_SQL_TEST") != "yes":
        raise RuntimeError("set MEDW_ALLOW_DISPOSABLE_SQL_TEST=yes only for a disposable instance")
    server = os.environ.get("MEDW_TEST_SQL_SERVER", "medw-sql-proof")
    secret = os.environ["MSSQL_SA_PASSWORD"]
    dsn = ("DRIVER={ODBC Driver 18 for SQL Server};"
           f"SERVER={server};UID=sa;PWD={secret};Encrypt=yes;TrustServerCertificate=yes;")
    master = None
    for _ in range(30):
        try:
            master = pyodbc.connect(dsn + "DATABASE=master", autocommit=True, timeout=2)
            break
        except pyodbc.Error:
            await asyncio.sleep(1)
    if master is None:
        raise RuntimeError("disposable SQL did not become available")
    database = "medw_verify_" + uuid.uuid4().hex
    master.execute(f"CREATE DATABASE [{database}]")
    connection = pyodbc.connect(dsn + f"DATABASE={database}", autocommit=False)
    directory = Path(__file__).parent / "sql"
    applied = apply(connection, directory)
    assert len(applied) == 4
    assert apply(connection, directory) == []
    with tempfile.TemporaryDirectory() as temporary:
        bad_directory = Path(temporary)
        for path in directory.glob("*.sql"):
            shutil.copy2(path, bad_directory / path.name)
        (bad_directory / "0005_injected_failure.sql").write_text(
            "CREATE TABLE dbo.must_rollback (id INT);\nGO\nSELECT * FROM dbo.missing_injected_table;")
        try:
            apply(connection, bad_directory)
        except pyodbc.Error:
            pass
        else:
            raise AssertionError("a failed migration was accepted")
        assert connection.execute("SELECT OBJECT_ID('dbo.must_rollback')").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM dbo.SchemaMigrations").fetchone()[0] == 4
    connection.execute("INSERT INTO core.study (study_id,sponsor) VALUES ('S1','synthetic')")
    connection.execute("INSERT INTO core.study_access (study_id,user_oid) VALUES ('S1','writer1')")
    connection.commit()
    eng = create_async_engine("mssql+aioodbc:///?odbc_connect=" + quote_plus(dsn + f"DATABASE={database}"))
    chunk = Chunk(id=str(uuid.uuid4()), study_id="S1", doc_id="d1", doc_type=DocType.tfl,
                  section_path="11.4", kind="prose", text="synthetic 12", ordinal=0,
                  source_revision="r1", source_location="page 1")
    generation = make_generation("S1", [chunk], parser_version="p7", embed_version="local-hash-000",
                                 embed_deployment="local-hash-000", embed_model_version="1", dimensions=4)
    citation = Citation(study_id="S1", chunk_id=chunk.id, source_revision="r1",
                        parser_version="p7", source_location="page 1")
    prov = Provenance("test", "generation", "a" * 40, "chat-deployment", "wrong-setting",
                      "sha256:" + "b" * 64, "7", "sha256:" + "c" * 64, "sha256:" + "d" * 64,
                      "chat-model", "2026-01-01", "embedding-model", "1",
                      deployment_revision="values-sha256:" + "f" * 64)
    event = generation_event(prov, generation, citations=[citation], correlation_id="e" * 32,
                              section_path="11.4", user_oid="writer1", output_text="Synthetic: 12 (8.5%).",
                              numeric_ok=True, structural_ok=False)
    sink = SqlAuditSink(eng)
    await sink.check()
    event_id = await sink.record_generation(event)
    async with eng.connect() as conn:
        result = (await conn.execute(text("SELECT " + ",".join(AUDIT_COLUMNS) +
                                          " FROM audit.generation_event WHERE event_id=:id"),
                                      {"id": event_id})).mappings().one()
        expected, _ = normalize_generation(event)
        for column in AUDIT_COLUMNS:
            assert str(result[column]).lower() == str(expected[column]).lower(), column
    access = SqlStudyAccess(eng)
    await access.check()
    assert await access.allowed("writer1", "S1")
    assert not await access.allowed("writer2", "S1")
    connection.execute("EXECUTE AS USER='id-medw-generation'")
    permissions = connection.execute(
        "SELECT HAS_PERMS_BY_NAME('audit.generation_event','OBJECT','INSERT'),"
        "HAS_PERMS_BY_NAME('audit.generation_event','OBJECT','UPDATE'),"
        "HAS_PERMS_BY_NAME('audit.generation_event','OBJECT','DELETE')").fetchone()
    assert tuple(permissions) == (1, 0, 0)
    for statement in ("DELETE FROM audit.generation_event", "UPDATE audit.generation_event SET user_oid='bad'"):
        try:
            connection.execute(statement)
        except pyodbc.Error:
            connection.rollback()
        else:
            raise AssertionError("runtime audit mutation unexpectedly permitted")
    connection.execute("REVERT")
    connection.commit()
    await eng.dispose()
    connection.close()
    master.execute(f"DROP DATABASE [{database}]")
    master.close()
    print(json.dumps({"migrations": len(applied), "repeat_applied": 0,
                      "failed_migration_rollback": "verified",
                      "audit_columns_read_back": len(AUDIT_COLUMNS),
                      "append_only_grants": "verified", "study_access": "verified",
                      "output_digest": hashlib.sha256(event["output_text"].encode()).hexdigest()}))


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""One-time schema/admin seeding under the signed-in Entra SQL administrator.

Run inside the generation image via scripts/azure.py; secrets arrive through
environment variables. Subsequent migrations run as id-medw-delivery, which has
DDL permissions separately from the runtime principals' restricted grants.
"""
from __future__ import annotations

import json
import os
import pathlib
import struct
import uuid

from migrate import apply


def main():
    import pyodbc
    config = json.loads(os.environ["MEDW_AZURE_BOOTSTRAP"])
    server, database = os.environ["MEDW_SQL_SERVER"], os.environ["MEDW_SQL_DATABASE"]
    if any(character in server + database for character in ";{}\r\n"):
        raise ValueError("Invalid SQL server/database")
    token = os.environ["MEDW_SQL_ACCESS_TOKEN"].encode("utf-16-le")
    connection = pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=tcp:{server},1433;"
        f"DATABASE={database};Encrypt=yes;TrustServerCertificate=no;",
        attrs_before={1256: struct.pack("<I", len(token)) + token}, timeout=30, autocommit=False)
    try:
        apply(connection, pathlib.Path(__file__).resolve().parents[1] / "db/sql")
        cursor = connection.cursor()
        sid = "0x" + uuid.UUID(config["migration_object_id"]).bytes_le.hex()
        cursor.execute("IF DATABASE_PRINCIPAL_ID('id-medw-delivery') IS NULL "
                       f"CREATE USER [id-medw-delivery] WITH SID={sid}, TYPE=E;")
        cursor.execute("ALTER ROLE db_owner ADD MEMBER [id-medw-delivery];")
        cursor.execute("IF NOT EXISTS (SELECT 1 FROM core.study WHERE study_id=?) "
                       "INSERT INTO core.study(study_id,sponsor,data_region) VALUES(?,?,?)",
                       config["study_id"], config["study_id"], "Operations verification", config["location"])
        cursor.execute("IF NOT EXISTS (SELECT 1 FROM core.e3_section WHERE section_path=?) "
                       "INSERT INTO core.e3_section(section_path,title,required) VALUES(?,?,1)",
                       config["section_path"], config["section_path"], "Placeholder efficacy section")
        cursor.execute("IF NOT EXISTS (SELECT 1 FROM core.study_access WHERE study_id=? AND user_oid=?) "
                       "INSERT INTO core.study_access(study_id,user_oid) VALUES(?,?)",
                       config["study_id"], config["writer_object_id"], config["study_id"], config["writer_object_id"])
        connection.commit()
        print("Schema migrated, delivery principal provisioned, test study membership seeded")
    finally:
        connection.close()


if __name__ == "__main__":
    main()

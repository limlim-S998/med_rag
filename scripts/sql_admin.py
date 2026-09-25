#!/usr/bin/env python3
"""Explicit SQL administration; schema execution belongs to Flyway.

Use a signed-in SQL administrator (DefaultAzureCredential), never a runtime
identity. Input is Terraform's non-secret `resources` output. Legacy adoption
only verifies the old ledger; it does not silently baseline a database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import struct
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ROLES = {
    "generation": ("id-medw-generation", "medw_generation"),
    "ingestion-worker": ("id-medw-ingestion", "medw_ingestion"),
    "gateway": ("id-medw-gateway", "medw_gateway"),
}


def connect(resources: dict):
    import pyodbc
    from azure.identity import DefaultAzureCredential

    server, database = resources["sql_server"], resources["sql_database"]
    if any(c in server + database for c in ";{}\r\n"):
        raise ValueError("Invalid SQL target")
    token_text = os.environ.get("MEDW_SQL_ACCESS_TOKEN")
    if not token_text:
        with DefaultAzureCredential() as credential:
            token_text = credential.get_token("https://database.windows.net/.default").token
    token = token_text.encode("utf-16-le")
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=tcp:{server},1433;"
        f"DATABASE={database};Encrypt=yes;TrustServerCertificate=no;",
        attrs_before={1256: struct.pack("<I", len(token)) + token}, timeout=30,
    )


def principal(cursor, name: str, client_id: str, role: str) -> None:
    # All SQL names below are constants, not caller-controlled identifiers.
    # External application SIDs use client IDs, not ARM principal/object IDs.
    sid = uuid.UUID(client_id).bytes_le
    existing = cursor.execute("SELECT sid FROM sys.database_principals WHERE name=?", name).fetchone()
    if existing and bytes(existing[0]) != sid:
        raise ValueError(f"{name} belongs to another identity; review and remove that user explicitly")
    if not existing:
        cursor.execute(f"CREATE USER [{name}] WITH SID=0x{sid.hex()}, TYPE=E")
    cursor.execute(f"ALTER ROLE [{role}] ADD MEMBER [{name}]")


def verify_legacy(cursor, directory: pathlib.Path = ROOT / "db/sql") -> dict:
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.glob("000[1-6]_*.sql")
    }
    actual = dict(cursor.execute("SELECT name,sha256 FROM dbo.SchemaMigrations").fetchall())
    if len(expected) != 6 or actual != expected:
        raise ValueError("Legacy migration ledger must contain exactly the six unchanged historical files")
    if cursor.execute("SELECT OBJECT_ID('dbo.flyway_schema_history')").fetchone()[0] is not None:
        raise ValueError("Flyway history already exists; use flyway validate, not baseline")
    # A ledger alone cannot establish arbitrary schema drift. Require these
    # application contracts and export the exact verified ledger for review.
    for table in ("core.study", "core.document", "core.study_access", "core.generated_draft",
                  "audit.generation_event", "audit.index_event", "audit.acceptance_event"):
        if cursor.execute("SELECT OBJECT_ID(?, 'U')", table).fetchone()[0] is None:
            raise ValueError(f"Missing legacy table: {table}")
    for column in ("job_id", "batch_id", "requested_by_oid"):
        if cursor.execute("SELECT COL_LENGTH('audit.index_event', ?)", column).fetchone()[0] is None:
            raise ValueError(f"Missing legacy audit column: {column}")
    return {"baseline_version": 6, "verified_sha256": actual,
            "next_step": "Review schema drift and this ledger; then explicitly run Flyway baseline at version 6"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--resources", type=pathlib.Path)
    target.add_argument("--cluster-config", type=pathlib.Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("delivery-principal", help="Initial provisioning before the first pipeline migration")
    commands.add_parser("runtime-principals", help="Run after migration 7 has created the runtime roles")
    commands.add_parser("verify-legacy", help="Read-only check; never automatically baselines")
    study = commands.add_parser("study")
    study.add_argument("--study", required=True)
    study.add_argument("--sponsor", required=True)
    study.add_argument("--region", required=True)
    member = commands.add_parser("membership")
    member.add_argument("--study", required=True)
    member.add_argument("--user-oid", type=uuid.UUID, required=True)
    member.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    if args.resources:
        resources = json.loads(args.resources.read_text())
    else:
        import yaml

        config = yaml.safe_load(args.cluster_config.read_text())["data"]
        resources = {"sql_server": config["SQL_SERVER"], "sql_database": config["SQL_DATABASE"],
                     "delivery_client_id": config["DELIVERY_CLIENT_ID"], "identities": {
                         name: {"client_id": config[name.upper().replace("-", "_") + "_CLIENT_ID"]}
                         for name in RUNTIME_ROLES}}
    connection = connect(resources)
    try:
        with connection:
            cursor = connection.cursor()
            if args.command == "delivery-principal":
                principal(cursor, "id-medw-delivery", resources["delivery_client_id"], "db_owner")
            elif args.command == "runtime-principals":
                for service, (name, role) in RUNTIME_ROLES.items():
                    principal(cursor, name, resources["identities"][service]["client_id"], role)
            elif args.command == "verify-legacy":
                print(json.dumps(verify_legacy(cursor), indent=2))
                return
            elif args.command == "study":
                if not 1 <= len(args.study) <= 32 or not 1 <= len(args.sponsor) <= 200:
                    parser.error("study must be 1-32 characters; sponsor 1-200")
                cursor.execute(
                    "IF NOT EXISTS (SELECT 1 FROM core.study WITH (UPDLOCK,HOLDLOCK) WHERE study_id=?) "
                    "INSERT core.study(study_id,sponsor,data_region) VALUES(?,?,?)",
                    args.study, args.study, args.sponsor, args.region)
            else:
                oid = str(args.user_oid)
                if args.revoke:
                    cursor.execute("UPDATE core.study_access SET revoked_at=SYSUTCDATETIME() "
                                   "WHERE study_id=? AND user_oid=?", args.study, oid)
                else:
                    cursor.execute(
                        "IF EXISTS (SELECT 1 FROM core.study_access WITH (UPDLOCK,HOLDLOCK) "
                        "WHERE study_id=? AND user_oid=?) "
                        "UPDATE core.study_access SET revoked_at=NULL WHERE study_id=? AND user_oid=? "
                        "ELSE INSERT core.study_access(study_id,user_oid) VALUES(?,?)",
                        args.study, oid, args.study, oid, args.study, oid)
        print(f"Completed explicit SQL operation: {args.command}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()

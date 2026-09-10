#!/usr/bin/env python3
"""Apply ordered SQL migrations once, under a deployment-only connection.

Set MEDW_SQL_CONNECTION_STRING (never a CLI password). Changed historical
migrations are rejected. GO batches share a transaction per migration file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def batches(sql: str) -> list[str]:
    if re.search(r"^\s*GO\s+\d+", sql, flags=re.IGNORECASE | re.MULTILINE):
        raise ValueError("GO repetition is unsupported")
    return [part.strip() for part in re.split(r"^\s*GO\s*(?:--[^\n]*)?$", sql,
                                               flags=re.IGNORECASE | re.MULTILINE) if part.strip()]


def apply(connection, directory: pathlib.Path) -> list[str]:
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ValueError("no ordered migrations found")
    cursor = connection.cursor()
    cursor.execute("""DECLARE @result int;
        EXEC @result = sys.sp_getapplock @Resource=N'medw-schema-migrations',
        @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=60000;
        SELECT @result;""")
    if cursor.fetchone()[0] < 0:
        raise RuntimeError("could not acquire migration lock")
    try:
        cursor.execute("""IF OBJECT_ID(N'dbo.SchemaMigrations', N'U') IS NULL
            CREATE TABLE dbo.SchemaMigrations (
              name nvarchar(255) NOT NULL PRIMARY KEY,
              sha256 char(64) NOT NULL,
              applied_at datetime2 NOT NULL DEFAULT SYSUTCDATETIME());""")
        connection.commit()
        known = dict(cursor.execute("SELECT name, sha256 FROM dbo.SchemaMigrations").fetchall())
        changes = [(p, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
        missing = set(known) - {path.name for path in files}
        if missing:
            raise ValueError(f"historical migration files removed: {sorted(missing)}")
        for path, digest in changes:
            if path.name in known and known[path.name] != digest:
                raise ValueError(f"historical migration changed: {path.name}")
        applied = []
        for path, digest in changes:
            if path.name in known:
                continue
            try:
                cursor.execute("SET XACT_ABORT ON;")
                for batch in batches(path.read_text()):
                    cursor.execute(batch)
                    while cursor.nextset():
                        pass
                cursor.execute("INSERT dbo.SchemaMigrations(name, sha256) VALUES (?, ?)",
                               path.name, digest)
                connection.commit()
                applied.append(path.name)
            except Exception:
                connection.rollback()
                raise
        return applied
    finally:
        connection.rollback()
        cursor.execute("EXEC sys.sp_releaseapplock @Resource=N'medw-schema-migrations', "
                       "@LockOwner='Session';")
        cursor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=pathlib.Path, default=ROOT / "db/sql")
    parser.add_argument("--connection-string-env", default="MEDW_SQL_CONNECTION_STRING")
    args = parser.parse_args()
    import pyodbc
    secret = os.environ.get(args.connection_string_env)
    if not secret:
        parser.error(f"{args.connection_string_env} must be supplied by the migration identity")
    connection = pyodbc.connect(secret, autocommit=False, timeout=30)
    try:
        for migration in apply(connection, args.directory):
            print(f"applied {migration}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()

"""Azure SQL adapters. Every new pooled connection obtains a fresh Entra token."""

from __future__ import annotations

import struct
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from medw_core.audit_events import (
    AUDIT_COLUMNS,
    INDEX_COLUMNS,
    normalize_generation,
    normalize_index,
)
from medw_core.persistence import Conflict
from medw_core.schemas import IndexGeneration
from medw_core.settings import Settings, require_setting

SQL_SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256


async def access_token_struct(cred) -> bytes:
    token = (await cred.get_token(SQL_SCOPE)).token.encode("utf-16-le")
    return struct.pack("<I", len(token)) + token


def engine(s: Settings, token: bytes | None = None, *, credential=None) -> AsyncEngine:
    server = require_setting(s.sql_server, "MEDW_SQL_SERVER")
    database = require_setting(s.sql_database, "MEDW_SQL_DATABASE")
    if credential is None and token is None:
        raise ValueError("SQL needs a credential")

    async def connect():
        import aioodbc
        fresh = await access_token_struct(credential) if credential is not None else token
        dsn = ("DRIVER={ODBC Driver 18 for SQL Server};"
               f"SERVER={server};DATABASE={database};Encrypt=yes;")
        return await aioodbc.connect(dsn=dsn, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: fresh},
                                     autocommit=False)

    return create_async_engine("mssql+aioodbc://", async_creator=connect,
                                pool_pre_ping=True, pool_recycle=1800)


AUDIT_INSERT = (
    "INSERT INTO audit.generation_event (" + ",".join(AUDIT_COLUMNS) + ") VALUES ("
    + ",".join(":" + column for column in AUDIT_COLUMNS) + ")"
)


class SqlAuditSink:
    def __init__(self, eng: AsyncEngine, evidence=None):
        self.engine, self.evidence = eng, evidence

    async def check(self) -> None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT HAS_PERMS_BY_NAME('audit.generation_event','OBJECT','INSERT'), "
                "COL_LENGTH('audit.generation_event','index_manifest')"))).first()
            if row is None or row[0] != 1 or row[1] is None:
                raise RuntimeError("audit schema or insert permission unavailable")

    async def record_generation(self, event: dict) -> str:
        row, citations = normalize_generation(event)
        if self.evidence is not None:
            await self.evidence.validate_selection(
                IndexGeneration.model_validate_json(row["index_manifest"]), citations)
            # Retain before SQL commit: failures may over-retain, never under-retain.
            await self.evidence.retain_for_event(row["event_id"], citations)
        async with self.engine.begin() as conn:
            await conn.execute(text(AUDIT_INSERT), row)
        return row["event_id"]

    async def record_index(self, event: dict) -> str:
        from sqlalchemy.exc import IntegrityError

        row = normalize_index(event)
        statement = ("INSERT INTO audit.index_event (" + ",".join(INDEX_COLUMNS) + ") VALUES ("
                     + ",".join(":" + column for column in INDEX_COLUMNS) + ")")
        try:
            async with self.engine.begin() as conn:
                if event.get("document") is not None:
                    import hashlib
                    import json

                    document = event["document"]
                    registry = {
                        "doc_id": hashlib.sha256(json.dumps(
                            [row["study_id"], row["doc_id"]], separators=(",", ":")).encode()).hexdigest(),
                        "study_id": row["study_id"], "doc_type": document["doc_type"],
                        "classifier_ver": document["classifier_version"],
                        "blob_path": document["blob_path"], "parser_version": row["parser_version"],
                    }
                    # The old schema's document key is global. A scoped hash
                    # preserves logical IDs in Cosmos/audit without cross-study
                    # collisions or a destructive primary-key migration.
                    present = (await conn.execute(text(
                        "SELECT doc_id FROM core.document WITH (UPDLOCK,HOLDLOCK) "
                        "WHERE doc_id=:doc_id"), registry)).first()
                    if present:
                        await conn.execute(text(
                            "UPDATE core.document SET doc_type=:doc_type,"
                            "classifier_ver=:classifier_ver,blob_path=:blob_path,"
                            "parser_version=:parser_version,ingested_at=SYSUTCDATETIME() "
                            "WHERE doc_id=:doc_id AND study_id=:study_id"), registry)
                    else:
                        await conn.execute(text(
                            "INSERT INTO core.document "
                            "(doc_id,study_id,doc_type,classifier_ver,blob_path,parser_version) "
                            "VALUES (:doc_id,:study_id,:doc_type,:classifier_ver,:blob_path,:parser_version)"),
                            registry)
                await conn.execute(text(statement), row)
        except IntegrityError:
            # A lost acknowledgement/recovered lease may repeat a completed
            # write. Only exactly the same event may reuse its immutable ID.
            async with self.engine.connect() as conn:
                existing = (await conn.execute(text(
                    "SELECT " + ",".join(INDEX_COLUMNS)
                    + " FROM audit.index_event WHERE event_id=:event_id"),
                    {"event_id": row["event_id"]})).mappings().first()
            if existing is None:
                raise
            stored = dict(existing)
            stored["event_id"] = str(uuid.UUID(str(stored["event_id"])))
            if stored != row:
                raise Conflict("index audit identity already contains different content") from None
        return row["event_id"]


class SqlStudyAccess:
    def __init__(self, eng: AsyncEngine):
        self.engine = eng

    async def allowed(self, user_id: str, study_id: str) -> bool:
        async with self.engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT 1 FROM core.study_access WHERE user_oid=:user_id "
                "AND study_id=:study_id AND revoked_at IS NULL"),
                {"user_id": user_id, "study_id": study_id})
            return result.first() is not None

    async def check(self) -> None:
        async with self.engine.connect() as conn:
            value = (await conn.execute(text(
                "SELECT HAS_PERMS_BY_NAME('core.study_access','OBJECT','SELECT')"))).scalar()
            if value != 1:
                raise RuntimeError("study authorization schema or SELECT permission unavailable")


async def record_generation(eng: AsyncEngine, event: dict) -> None:
    await SqlAuditSink(eng).record_generation(event)

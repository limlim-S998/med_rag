"""Azure SQL adapters. Every new pooled connection obtains a fresh Entra token."""

from __future__ import annotations

import struct

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from medw_core.audit_events import AUDIT_COLUMNS, normalize_generation
from medw_core.schemas import IndexGeneration
from medw_core.settings import Settings

SQL_SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256


async def access_token_struct(cred) -> bytes:
    token = (await cred.get_token(SQL_SCOPE)).token.encode("utf-16-le")
    return struct.pack("<I", len(token)) + token


def engine(s: Settings, token: bytes | None = None, *, credential=None) -> AsyncEngine:
    if not s.sql_server:
        raise RuntimeError("MEDW_SQL_SERVER is not set; the audit sink is unavailable")
    if credential is None and token is None:
        raise ValueError("SQL needs a credential")

    async def connect():
        import aioodbc
        fresh = await access_token_struct(credential) if credential is not None else token
        dsn = ("DRIVER={ODBC Driver 18 for SQL Server};"
               f"SERVER={s.sql_server};DATABASE={s.sql_database};Encrypt=yes;")
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
        import uuid
        row = {"event_id": str(uuid.uuid4()), **event}
        required = ("event_id", "study_id", "doc_id", "parser_version", "embed_version",
                    "collection", "chunks_upserted", "index_generation_id", "source_revision")
        if any(row.get(k) is None for k in required):
            raise ValueError("complete source/index provenance required")
        statement = ("INSERT INTO audit.index_event (" + ",".join(required) + ") VALUES ("
                     + ",".join(":" + c for c in required) + ")")
        async with self.engine.begin() as conn:
            await conn.execute(text(statement), row)
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

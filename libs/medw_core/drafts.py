"""Persistent draft references; acceptance never mutates the generation audit."""

import uuid

from medw_core.persistence import Conflict


def draft_from_event(event: dict) -> dict:
    return {"draft_id": event["event_id"], "event_id": event["event_id"],
            "study_id": event["study_id"], "section_path": event["section_path"],
            "created_by_oid": event["user_oid"], "status": "draft",
            "accepted_by_oid": None, "output_sha256": event["output_sha256"]}


class SqlDraftStore:
    def __init__(self, engine):
        self.engine = engine

    async def check(self) -> None:
        from sqlalchemy import text
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT TOP (0) draft_id FROM core.generated_draft"))

    async def create(self, event: dict) -> dict:
        from sqlalchemy import text
        draft = draft_from_event(event)
        async with self.engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO core.generated_draft
                    (draft_id, event_id, study_id, section_path, created_by_oid, output_sha256)
                VALUES (:draft_id,:event_id,:study_id,:section_path,:created_by_oid,:output_sha256)
            """), draft)
        return draft

    async def get(self, study_id: str, section_path: str, draft_id: str) -> dict | None:
        from sqlalchemy import text
        async with self.engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT CONVERT(VARCHAR(36),draft_id) AS draft_id,
                       CONVERT(VARCHAR(36),event_id) AS event_id, study_id, section_path,
                       created_by_oid,status,accepted_by_oid,output_sha256
                FROM core.generated_draft
                WHERE study_id=:study AND section_path=:section AND draft_id=:draft
            """), {"study": study_id, "section": section_path, "draft": draft_id})).mappings().first()
        if row is None:
            return None
        result = dict(row)
        for field in ("draft_id", "event_id"):
            result[field] = str(uuid.UUID(str(result[field])))
        return result

    async def accept(self, study_id: str, section_path: str, draft_id: str,
                     user_oid: str, correlation_id: str) -> dict:
        from sqlalchemy import text
        async with self.engine.begin() as connection:
            result = await connection.execute(text("""
                EXEC core.accept_generated_draft @study_id=:study,
                    @section_path=:section,@draft_id=:draft,@user_oid=:user,
                    @correlation_id=:correlation
            """), {"study": study_id, "section": section_path, "draft": draft_id,
                   "user": user_oid, "correlation": correlation_id})
            outcome = result.scalar_one()
            if outcome == "missing":
                raise LookupError("draft not found")
            if outcome == "conflict":
                raise Conflict("draft was already accepted by another writer")
        draft = await self.get(study_id, section_path, draft_id)
        if draft is None:
            raise RuntimeError("accepted draft disappeared")
        return draft

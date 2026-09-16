"""Persistent draft references; acceptance never mutates the generation audit."""

import json
import sqlite3
import uuid
from pathlib import Path

from medw_core.persistence import Conflict


def draft_from_event(event: dict) -> dict:
    return {"draft_id": event["event_id"], "event_id": event["event_id"],
            "study_id": event["study_id"], "section_path": event["section_path"],
            "created_by_oid": event["user_oid"], "status": "draft",
            "accepted_by_oid": None, "output_sha256": event["output_sha256"]}


class SQLiteDraftStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS generated_draft (
                draft_id TEXT PRIMARY KEY REFERENCES platform_audit(event_id), study_id TEXT NOT NULL,
                section_path TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS draft_acceptance (
                draft_id TEXT PRIMARY KEY, user_oid TEXT NOT NULL,
                correlation_id TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_acceptance_update
                BEFORE UPDATE ON draft_acceptance
                BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_acceptance_delete
                BEFORE DELETE ON draft_acceptance
                BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        """)

    async def check(self) -> None:
        self.connection.execute("SELECT draft_id FROM generated_draft LIMIT 1").fetchone()

    async def close(self) -> None:
        self.connection.close()

    async def create(self, event: dict) -> dict:
        draft = draft_from_event(event)
        existing = await self.get(draft["study_id"], draft["section_path"], draft["draft_id"])
        if existing is not None:
            if any(existing[key] != draft[key] for key in
                   ("created_by_oid", "output_sha256", "event_id")):
                raise Conflict("draft identity already contains different output")
            return existing
        self.connection.execute("INSERT INTO generated_draft VALUES (?,?,?,?)", (
            draft["draft_id"], draft["study_id"], draft["section_path"], json.dumps(draft)))
        return draft

    async def get(self, study_id: str, section_path: str, draft_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM generated_draft WHERE study_id=? AND section_path=? AND draft_id=?",
            (study_id, section_path, draft_id)).fetchone()
        return json.loads(row[0]) if row else None

    async def accept(self, study_id: str, section_path: str, draft_id: str,
                     user_oid: str, correlation_id: str) -> dict:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            draft = await self.get(study_id, section_path, draft_id)
            if draft is None:
                raise LookupError("draft not found")
            if draft["status"] == "accepted":
                if draft["accepted_by_oid"] != user_oid:
                    raise Conflict("draft was already accepted by another writer")
            else:
                draft.update(status="accepted", accepted_by_oid=user_oid)
                self.connection.execute(
                    "UPDATE generated_draft SET payload=? WHERE draft_id=?",
                    (json.dumps(draft), draft_id))
                self.connection.execute("INSERT INTO draft_acceptance VALUES (?,?,?)",
                                        (draft_id, user_oid, correlation_id))
            self.connection.execute("COMMIT")
            return draft
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise


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

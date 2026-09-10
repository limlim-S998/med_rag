"""Durable local audit; SQLite triggers prevent update/delete on audit rows."""

import json
import sqlite3
import uuid
from pathlib import Path

from medw_core.audit_events import normalize_generation
from medw_core.schemas import IndexGeneration


class SQLiteAuditSink:
    def __init__(self, path: str | Path, evidence=None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.evidence = evidence
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS platform_audit (
                event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, event_json TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON platform_audit
                BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON platform_audit
                BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        """)

    async def check(self) -> None:
        self.connection.execute("SELECT 1").fetchone()

    async def close(self) -> None:
        self.connection.close()

    async def record_generation(self, event: dict) -> str:
        row, citations = normalize_generation(event)
        if self.evidence is not None:
            await self.evidence.validate_selection(
                IndexGeneration.model_validate_json(row["index_manifest"]), citations)
            await self.evidence.retain_for_event(row["event_id"], citations)
        self.connection.execute("INSERT INTO platform_audit VALUES (?,?,?)",
                                (row["event_id"], "generation", json.dumps(row)))
        return row["event_id"]

    async def record_index(self, event: dict) -> str:
        row = {"event_id": str(uuid.uuid4()), **event}
        self.connection.execute("INSERT INTO platform_audit VALUES (?,?,?)",
                                (row["event_id"], "index", json.dumps(row)))
        return row["event_id"]

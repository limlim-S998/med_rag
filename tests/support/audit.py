"""Offline audit and draft stores; real SQL permissions are verified separately."""

import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

from medw_core.audit_events import normalize_generation, normalize_index
from medw_core.drafts import draft_from_event
from medw_core.persistence import Conflict
from medw_core.schemas import IndexGeneration


class InMemoryAuditSink:
    """Satisfies medw_core.ports.AuditSink. Append-only, like the real one."""

    def __init__(self):
        self._generations: list[dict] = []
        self._index: list[dict] = []

    @property
    def generations(self) -> tuple[dict, ...]:
        # A tuple, so a caller cannot append to the trail through the getter.
        return tuple(deepcopy(self._generations))

    @property
    def index_events(self) -> tuple[dict, ...]:
        return tuple(deepcopy(self._index))

    async def record_generation(self, event: dict) -> str:
        event_id = str(uuid.uuid4())
        self._generations.append(deepcopy({"event_id": event_id, **event}))
        return event_id

    async def record_index(self, event: dict) -> str:
        event_id = str(uuid.uuid4())
        self._index.append(deepcopy({"event_id": event_id, **event}))
        return event_id


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
        row = normalize_index(event)
        try:
            self.connection.execute("INSERT INTO platform_audit VALUES (?,?,?)",
                                    (row["event_id"], "index", json.dumps(row)))
        except sqlite3.IntegrityError:
            existing = self.connection.execute(
                "SELECT kind,event_json FROM platform_audit WHERE event_id=?",
                (row["event_id"],)).fetchone()
            if existing is None:
                raise
            if existing[0] != "index" or json.loads(existing[1]) != row:
                raise Conflict("index audit identity already contains different content") from None
        return row["event_id"]


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


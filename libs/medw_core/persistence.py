"""Small conditional document store shared by platform state machines.

SQLite is durable local infrastructure, not the Azure production database.
Cosmos uses the same compare-and-swap contract with service-side ETags.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from dataclasses import dataclass
from typing import Protocol


class Conflict(RuntimeError):
    """The record changed since it was read, or already exists."""


@dataclass(frozen=True)
class Record:
    value: dict
    revision: str


class StateStore(Protocol):
    async def get(self, kind: str, study_id: str, key: str) -> Record | None: ...

    async def put(self, kind: str, study_id: str, key: str, value: dict, *,
                  expected_revision: str | None) -> Record: ...

    async def list(self, kind: str, study_id: str) -> list[Record]: ...


class SQLiteStateStore:
    def __init__(self, path: str | pathlib.Path):
        if str(path) != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS platform_state (
            kind TEXT NOT NULL, study_id TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(kind, study_id, key))""")

    async def close(self) -> None:
        self.connection.close()

    async def check(self) -> None:
        self.connection.execute("SELECT 1").fetchone()

    async def get(self, kind: str, study_id: str, key: str) -> Record | None:
        row = self.connection.execute(
            "SELECT value,revision FROM platform_state WHERE kind=? AND study_id=? AND key=?",
            (kind, study_id, key),
        ).fetchone()
        return Record(json.loads(row[0]), str(row[1])) if row else None

    async def put(self, kind: str, study_id: str, key: str, value: dict, *,
                  expected_revision: str | None) -> Record:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"))
        try:
            if expected_revision is None:
                self.connection.execute(
                    "INSERT INTO platform_state VALUES (?,?,?,?,1)", (kind, study_id, key, body))
                revision = "1"
            else:
                result = self.connection.execute(
                    "UPDATE platform_state SET value=?,revision=revision+1 "
                    "WHERE kind=? AND study_id=? AND key=? AND revision=?",
                    (body, kind, study_id, key, int(expected_revision)),
                )
                if result.rowcount != 1:
                    raise Conflict("stale state revision")
                revision = str(int(expected_revision) + 1)
        except sqlite3.IntegrityError as exc:
            raise Conflict("state record already exists") from exc
        return Record(json.loads(body), revision)

    async def list(self, kind: str, study_id: str) -> list[Record]:
        return [Record(json.loads(body), str(revision)) for body, revision in
                self.connection.execute(
                    "SELECT value,revision FROM platform_state WHERE kind=? AND study_id=?",
                    (kind, study_id)).fetchall()]


"""Small conditional document store shared by platform state machines.

Cosmos implements compare-and-swap with service-side ETags. Offline tests
provide a SQLite implementation from tests/support, outside the runtime package.
"""

from __future__ import annotations

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

    async def list(self, kind: str, study_id: str | None = None) -> list[Record]: ...


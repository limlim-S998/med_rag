"""Persistent local implementations of session, membership and metadata ports."""

import time

from medw_core.persistence import Conflict, StateStore


async def _put(state: StateStore, kind: str, partition: str, key: str, value: dict) -> None:
    for _ in range(5):
        old = await state.get(kind, partition, key)
        try:
            await state.put(kind, partition, key, value,
                            expected_revision=old.revision if old else None)
            return
        except Conflict:
            continue
    raise Conflict("record kept changing during update")


class PersistentSessionStore:
    def __init__(self, state: StateStore, ttl_seconds: int = 3600):
        self.state = state
        self.ttl_seconds = ttl_seconds

    async def get(self, user_id: str, session_id: str) -> dict | None:
        record = await self.state.get("session", user_id, session_id)
        if record is None or record.value["expires_at"] <= time.time():
            return None
        return record.value["session"]

    async def put(self, session: dict) -> None:
        user_id, session_id = session["user_id"], session["session_id"]
        if not user_id or not session_id:
            raise ValueError("user_id and session_id are required")
        await _put(self.state, "session", user_id, session_id,
                   {"session": session, "expires_at": time.time() + self.ttl_seconds})


class PersistentDocumentStore:
    def __init__(self, state: StateStore):
        self.state = state

    async def upsert(self, doc: dict) -> dict:
        await _put(self.state, "document_metadata", doc["study_id"], doc["doc_id"], doc)
        return dict(doc)

    async def by_study(self, study_id: str) -> list[dict]:
        return [record.value for record in await self.state.list("document_metadata", study_id)]


class LocalStudyAccess:
    """Membership is explicitly seeded by local tests/tools, never auto-granted."""

    def __init__(self, state: StateStore):
        self.state = state

    async def allowed(self, user_id: str, study_id: str) -> bool:
        record = await self.state.get("membership", study_id, user_id)
        return record is not None and record.value.get("active") is True

    async def grant(self, user_id: str, study_id: str) -> None:
        await _put(self.state, "membership", study_id, user_id, {"active": True})

    async def revoke(self, user_id: str, study_id: str) -> None:
        await _put(self.state, "membership", study_id, user_id, {"active": False})

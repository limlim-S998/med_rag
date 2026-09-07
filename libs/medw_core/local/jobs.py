# Ingestion job state in a dict.
#
# Cosmos in the cloud; this in tests and in MEDW_BACKEND=local. The state
# machine's rules live with the job model, not with the store - so the same
# illegal-transition guard applies whichever backend is wired in. A store that
# enforced its own rules would let the two drift.

import uuid
from datetime import UTC, datetime
from typing import Any


class InMemoryJobStore:
    """Satisfies medw_core.ports.JobStore. Not durable, by construction."""

    def __init__(self):
        self.items: dict[str, dict[str, Any]] = {}

    async def create(self, study_id: str, doc_id: str) -> dict:
        job: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "study_id": study_id,
            "doc_id": doc_id,
            "state": "queued",
            "history": ["queued"],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.items[job["id"]] = job
        return job

    async def advance(self, job: dict, state: str) -> dict:
        job["state"] = state
        job["history"].append(state)
        job["updated_at"] = datetime.now(UTC).isoformat()
        self.items[job["id"]] = job
        return job

    async def fail(self, job: dict, state: str, error: str) -> dict:
        job["state"] = "failed"
        job["failed_at_state"] = state
        job["error"] = error
        job["history"].append("failed")
        self.items[job["id"]] = job
        return job

    async def get(self, study_id: str, job_id: str) -> dict | None:
        job = self.items.get(job_id)
        # Scoped by study even in memory: the Cosmos read is partitioned by
        # /study_id, and a local store that ignored it would let a bug pass
        # here and fail in the cloud.
        return job if job and job["study_id"] == study_id else None

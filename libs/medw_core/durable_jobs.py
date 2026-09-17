"""Durable ingestion metadata with ETag/SQLite CAS and expiring worker leases."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable

from medw_core.jobs import PROCESSING_STATES, TERMINAL, JobState, check_transition
from medw_core.persistence import Conflict, Record, StateStore


class DurableJobStore:
    def __init__(self, state: StateStore, *, clock: Callable[[], float] = time.time):
        self.state, self.clock = state, clock

    @staticmethod
    def _job(record: Record) -> dict:
        return {**record.value, "revision": record.revision}

    async def create(self, study_id: str, doc_id: str, *,
                     source_revision: str | None = None,
                     idempotency_key: str | None = None,
                     correlation_id: str | None = None, processing: str = "immediate",
                     requested_by_oid: str | None = None) -> dict:
        if processing not in {"immediate", "nightly"}:
            raise ValueError("unknown processing choice")
        job_id = (hashlib.sha256(idempotency_key.encode()).hexdigest()
                  if idempotency_key else str(uuid.uuid4()))
        initial = "scheduled" if processing == "nightly" else "queued"
        body: dict = {"id": job_id, "study_id": study_id, "doc_id": doc_id,
                "source_revision": source_revision, "state": initial, "history": [initial],
                "processing": processing, "batch_id": None, "requested_by_oid": requested_by_oid,
                "checkpoints": {}, "lease_owner": None, "lease_until": 0,
                "created_at": self.clock(), "updated_at": self.clock(), "correlation_id": correlation_id or uuid.uuid4().hex,
                "attempts": 0, "next_attempt_at": 0}
        try:
            return self._job(await self.state.put("job", study_id, job_id, body,
                                                 expected_revision=None))
        except Conflict:
            existing = await self.get(study_id, job_id)
            if (not existing or existing["doc_id"] != doc_id or
                    existing["source_revision"] != source_revision or
                    existing.get("processing", "immediate") != processing):
                raise Conflict("idempotency key reused for different input") from None
            return existing

    async def get(self, study_id: str, job_id: str) -> dict | None:
        record = await self.state.get("job", study_id, job_id)
        return self._job(record) if record else None

    async def admit_batch(self, study_id: str, job_id: str, batch_id: str) -> dict:
        """CAS admission makes a frozen Airflow selection safe to dispatch again."""
        job = await self.get(study_id, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.get("batch_id") == batch_id:
            return job
        if job["state"] != "scheduled" or job.get("batch_id"):
            raise Conflict("job is no longer awaiting this batch")
        return await self._save(job, state="queued", batch_id=batch_id,
                                history=[*job["history"], "queued"])

    async def _save(self, job: dict, **changes) -> dict:
        body = {k: v for k, v in job.items() if k != "revision"}
        body.update(changes, updated_at=self.clock())
        return self._job(await self.state.put("job", job["study_id"], job["id"], body,
                                             expected_revision=job["revision"]))

    def _lease(self, job: dict) -> None:
        if not job.get("lease_owner") or job["lease_until"] <= self.clock():
            raise Conflict("job has no live worker lease")
        if JobState(job["state"]) in TERMINAL:
            raise Conflict("terminal jobs cannot be changed")

    async def claim(self, study_id: str, job_id: str, worker_id: str, *,
                    lease_seconds: float = 60) -> dict:
        if lease_seconds <= 0 or not worker_id:
            raise ValueError("worker and positive lease duration required")
        job = await self.get(study_id, job_id)
        if not job:
            raise KeyError(job_id)
        if JobState(job["state"]) in TERMINAL:
            raise Conflict("terminal jobs cannot be claimed")
        if job["state"] == "scheduled":
            raise Conflict("scheduled jobs require Airflow admission")
        if job["lease_until"] > self.clock():
            raise Conflict("job is already leased")
        return await self._save(job, lease_owner=worker_id,
                                lease_until=self.clock() + lease_seconds)

    async def renew(self, job: dict, *, lease_seconds: float = 60) -> dict:
        self._lease(job)
        if lease_seconds <= 0:
            raise ValueError("positive lease duration required")
        return await self._save(job, lease_until=self.clock() + lease_seconds)

    async def advance(self, job: dict, state: str) -> dict:
        self._lease(job)
        check_transition(JobState(job["state"]), JobState(state))
        return await self._save(job, state=state, history=[*job["history"], state])

    async def checkpoint(self, job: dict, name: str, artifact_uri: str) -> dict:
        self._lease(job)
        if not name or not artifact_uri:
            raise ValueError("checkpoint name and immutable artifact reference required")
        previous = job["checkpoints"].get(name)
        if previous and previous != artifact_uri:
            raise Conflict("a committed checkpoint cannot be rewritten")
        return await self._save(job, checkpoints={**job["checkpoints"], name: artifact_uri})

    async def fail(self, job: dict, state: str, error: str) -> dict:
        self._lease(job)
        check_transition(JobState(job["state"]), JobState.failed)
        return await self._save(job, state="failed", failed_at_state=state, error=error,
                                history=[*job["history"], "failed"])

    async def recoverable(self, study_id: str | None = None) -> list[dict]:
        return [self._job(r) for r in await self.state.list("job", study_id)
                if JobState(r.value["state"]) not in TERMINAL | {JobState.scheduled}
                and r.value["lease_until"] <= self.clock()
                and r.value.get("next_attempt_at", 0) <= self.clock()]


async def run_stages(store: DurableJobStore, job: dict, worker_id: str,
                     stages: dict[str, Callable[[dict], Awaitable[str]]]) -> dict:
    """Run supplied idempotent stages; clinical callbacks remain outside this module.

    A crash after checkpoint persistence resumes without re-running that callback.
    Long callbacks must renew their lease; an expired worker cannot commit work.
    """
    job = await store.claim(job["study_id"], job["id"], worker_id)
    for stage in PROCESSING_STATES:
        if stage.value not in stages:
            raise ValueError(f"missing stage callback: {stage}")
        if stage.value in job["checkpoints"]:
            continue
        if job["state"] != stage:
            job = await store.advance(job, stage.value)
        try:
            artifact = await stages[stage.value](job)
            job = await store.checkpoint(job, stage.value, artifact)
        except Exception as exc:
            await store.fail(job, stage.value, str(exc))
            raise
    return await store.advance(job, JobState.done.value)

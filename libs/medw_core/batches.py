"""Frozen batch selections and restart-safe admission to the durable worker.

Airflow owns scheduling and task execution. Cosmos owns batch membership and
document outcomes, so retrying a DAG never discovers a different set of inputs.
"""

from __future__ import annotations

import hashlib
import time

from medw_core.durable_jobs import DurableJobStore
from medw_core.persistence import Conflict, StateStore

PARTITION = "__airflow_batches__"


class BatchStore:
    def __init__(self, state: StateStore, jobs: DurableJobStore, *, limit: int = 500, clock=time.time):
        self.state, self.jobs, self.limit, self.clock = state, jobs, limit, clock

    async def prepare(self, run_id: str, cutoff: float, actor: str, correlation_id: str) -> dict:
        if cutoff > self.clock() + 60 or cutoff <= 0:
            raise ValueError("batch cutoff must be a past scheduling boundary")
        batch_id = hashlib.sha256(("ingest_study/" + run_id).encode()).hexdigest()
        existing = await self.state.get("batch", PARTITION, batch_id)
        if existing is None:
            pending = sorted((r.value for r in await self.state.list("job")
                              if r.value["state"] == "scheduled" and r.value["created_at"] < cutoff),
                             key=lambda job: (job["created_at"], job["study_id"], job["id"]))
            value = {"id": batch_id, "run_id": run_id, "cutoff": cutoff,
                     "created_at": self.clock(), "coordinator_oid": actor,
                     "correlation_id": correlation_id,
                     "items": [{"study_id": job["study_id"], "job_id": job["id"]}
                               for job in pending[:self.limit]],
                     "deferred_by_limit": max(0, len(pending) - self.limit)}
            try:
                existing = await self.state.put("batch", PARTITION, batch_id, value,
                                                expected_revision=None)
            except Conflict:
                existing = await self.state.get("batch", PARTITION, batch_id)
        if existing is None:
            raise Conflict("batch selection unavailable")
        if existing.value["cutoff"] != cutoff:
            raise Conflict("run identifier reused with another cutoff")
        for item in existing.value["items"]:
            try:
                await self.jobs.admit_batch(item["study_id"], item["job_id"], batch_id)
            except Conflict:
                current = await self.jobs.get(item["study_id"], item["job_id"])
                # Another overlapping DAG may have admitted the same input.
                # It is explicitly excluded here, never processed twice.
                if not current or not current.get("batch_id"):
                    raise
        return await self.status(batch_id)

    async def status(self, batch_id: str, *, study_id: str | None = None) -> dict:
        row = await self.state.get("batch", PARTITION, batch_id)
        if row is None:
            raise KeyError(batch_id)
        selected = [item for item in row.value["items"]
                    if study_id is None or item["study_id"] == study_id]
        if study_id is not None and not selected:
            raise KeyError(batch_id)
        counts = {name: 0 for name in ("waiting", "running", "done", "failed", "superseded", "excluded")}
        items = []
        for item in selected:
            job = await self.jobs.get(item["study_id"], item["job_id"])
            if job is None:
                raise RuntimeError("batch references a missing durable job")
            if job.get("batch_id") not in (None, batch_id):
                outcome = "excluded"
            elif job["state"] == "scheduled":
                outcome = "waiting"
            elif job["state"] in ("done", "failed", "superseded"):
                outcome = job["state"]
            else:
                outcome = "running"
            counts[outcome] += 1
            items.append({**item, "state": job["state"], "outcome": outcome,
                          "source_revision": job["source_revision"],
                          "correlation_id": job["correlation_id"]})
        status = ("running" if counts["waiting"] + counts["running"] else
                  "failed" if counts["failed"] else "completed")
        return {"id": batch_id, "run_id": row.value["run_id"], "cutoff": row.value["cutoff"],
                "status": status, "counts": counts, "items": items,
                **({"deferred_by_limit": row.value["deferred_by_limit"]} if study_id is None else {})}

"""Ingestion entrypoint contract.

Durable leases, checkpoints and retries live in the shared library so batch
and request-triggered workers cannot disagree. The reusable runner performs the current deterministic placeholder processing.
"""

from medw_core.durable_jobs import DurableJobStore as JobStore
from medw_core.durable_jobs import run_stages
from medw_core.jobs import RETRY_COST, JobState, attempts_for, is_expensive

State = JobState
__all__ = ["RETRY_COST", "JobStore", "State", "attempts_for", "is_expensive",
           "run_ingest", "run_stages"]


async def run_ingest(ctx: dict, job: dict) -> None:
    runner = ctx["ingestion"]
    lease = await runner.claim(job)
    await runner.run(lease)

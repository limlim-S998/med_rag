"""Ingestion entrypoint contract.

Durable leases, checkpoints and retries live in the shared library so batch
and request-triggered workers cannot disagree. Clinical stage callbacks are
still held back; synthetic callbacks exercise run_stages in the platform tests.
"""

from medw_core.durable_jobs import DurableJobStore as JobStore
from medw_core.durable_jobs import run_stages
from medw_core.jobs import RETRY_COST, JobState, attempts_for, is_expensive

State = JobState
__all__ = ["RETRY_COST", "JobStore", "State", "attempts_for", "is_expensive",
           "run_ingest", "run_stages"]


async def run_ingest(ctx: dict, job: dict) -> None:
    raise NotImplementedError("clinical ingestion stage callbacks are intentionally held back")

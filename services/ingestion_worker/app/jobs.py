# Ingestion as an explicit state machine in Cosmos.
#
# Explicit because "it failed somewhere in ingestion" is not an operable
# message. Each transition is a small write to the jobs container, partitioned
# by /study_id, so the status endpoint is a single-partition point read and
# the writer sees which stage is running.
#
# Every stage is idempotent, so recovery is "re-run from the last completed
# state" rather than "clean up and start over". That property is bought by the
# deterministic IDs, and it is the reason the whole pipeline can be retried
# blind after a parser fix.

from medw_core.jobs import RETRY_COST, JobState, attempts_for, is_expensive

# State, transitions and retry costs live in medw_core.jobs, shared with the
# in-memory store and with the Airflow DAG. They were defined here, which meant
# the local backend and Cosmos could disagree about what a legal path was.
State = JobState

# Re-exported so the recovery decision is greppable from the service that
# makes it: resume in place, or start over?
__all__ = ["RETRY_COST", "JobStore", "State", "attempts_for", "is_expensive", "run_ingest"]


class JobStore:
    def __init__(self, container):
        self.c = container

    async def create(self, study_id: str, doc_id: str) -> dict:
        ...

    async def advance(self, job: dict, state: State) -> dict:
        ...

    async def fail(self, job: dict, state: State, error: str) -> dict:
        ...


async def run_ingest(ctx: dict, job: dict) -> None:
    # The single-document path, straight-line. The DAG runs these same calls
    # as separate tasks so Airflow can retry and parallelise them; here they
    # are awaited in order because there is one document and a writer waiting.
    ...

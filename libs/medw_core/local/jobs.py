"""Ephemeral unit-test store using exactly the durable job state machine.

Runtime local services use DurableJobStore(SQLiteStateStore(path)); this
in-memory variant deliberately cannot survive process exit.
"""

from medw_core.durable_jobs import DurableJobStore
from medw_core.persistence import SQLiteStateStore


class InMemoryJobStore(DurableJobStore):
    def __init__(self):
        super().__init__(SQLiteStateStore(":memory:"))

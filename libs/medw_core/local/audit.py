# The audit trail, in a list.
#
# Note what it does NOT have: any way to modify or remove a recorded event.
# That mirrors db/sql/0003_grants.sql, where the service principal is granted
# INSERT and deliberately not UPDATE or DELETE. If the local implementation
# allowed mutation, a test could pass here and the behaviour would be
# impossible against the real store - the fake would be lying about the
# contract rather than standing in for it.

import uuid
from copy import deepcopy


class InMemoryAuditSink:
    """Satisfies medw_core.ports.AuditSink. Append-only, like the real one."""

    def __init__(self):
        self._generations: list[dict] = []
        self._index: list[dict] = []

    @property
    def generations(self) -> tuple[dict, ...]:
        # A tuple, so a caller cannot append to the trail through the getter.
        return tuple(deepcopy(self._generations))

    @property
    def index_events(self) -> tuple[dict, ...]:
        return tuple(deepcopy(self._index))

    async def record_generation(self, event: dict) -> str:
        event_id = str(uuid.uuid4())
        self._generations.append(deepcopy({"event_id": event_id, **event}))
        return event_id

    async def record_index(self, event: dict) -> str:
        event_id = str(uuid.uuid4())
        self._index.append(deepcopy({"event_id": event_id, **event}))
        return event_id

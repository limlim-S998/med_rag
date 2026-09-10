import copy

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

from medw_core.cosmos_state import CosmosStateStore
from medw_core.durable_jobs import DurableJobStore
from medw_core.persistence import Conflict


class CosmosDouble:
    """Simulates the SDK's service-side ETag condition, not production availability."""

    def __init__(self):
        self.items = {}
        self.version = 0

    async def read_item(self, item, *, partition_key):
        if (partition_key, item) not in self.items:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return copy.deepcopy(self.items[(partition_key, item)])

    async def create_item(self, doc):
        key = doc["study_id"], doc["id"]
        if key in self.items:
            raise CosmosHttpResponseError(status_code=409, message="already exists")
        return self.save(doc)

    def save(self, doc):
        self.version += 1
        result = {**copy.deepcopy(doc), "_etag": str(self.version)}
        self.items[(doc["study_id"], doc["id"])] = result
        return copy.deepcopy(result)

    async def replace_item(self, item, doc, *, etag, match_condition):
        assert match_condition == MatchConditions.IfNotModified
        current = self.items[(doc["study_id"], item)]
        if etag != current["_etag"]:
            raise CosmosHttpResponseError(status_code=412, message="precondition failed")
        return self.save(doc)

    def query_items(self, *, query, parameters, partition_key):
        kind = parameters[0]["value"]

        async def rows():
            for (study, _), row in self.items.items():
                if study == partition_key and row["kind"] == kind:
                    yield copy.deepcopy(row)
        return rows()


async def test_cosmos_jobs_enforce_same_lease_cas_and_partition_contract():
    client = CosmosDouble()
    clock = [1.0]
    jobs = DurableJobStore(CosmosStateStore(client), clock=lambda: clock[0])
    job = await jobs.create("S1", "doc", source_revision="revision")
    assert await jobs.get("S2", job["id"]) is None
    leased = await jobs.claim("S1", job["id"], "worker1", lease_seconds=1)
    current = await jobs.advance(leased, "extracting")
    with pytest.raises(Conflict):
        await jobs.advance(leased, "extracting")
    current = await jobs.checkpoint(current, "extracting", "sha256:layout")
    clock[0] = 3
    reopened = DurableJobStore(CosmosStateStore(client), clock=lambda: clock[0])
    assert len(await reopened.recoverable("S1")) == 1
    resumed = await reopened.claim("S1", job["id"], "worker2")
    assert resumed["checkpoints"] == {"extracting": "sha256:layout"}
    with pytest.raises(Conflict):
        await jobs.advance(current, "classifying")

"""Cosmos ETag implementation of the durable platform-state contract."""

import hashlib
import json

from medw_core.persistence import Conflict, Record


class CosmosStateStore:
    """Use a dedicated platform_state container partitioned on /study_id, without TTL."""

    def __init__(self, container):
        self.container = container

    @staticmethod
    def _id(kind: str, key: str) -> str:
        return hashlib.sha256(json.dumps([kind, key]).encode()).hexdigest()

    async def check(self) -> None:
        await self.container.read()

    async def get(self, kind: str, study_id: str, key: str) -> Record | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            doc = await self.container.read_item(self._id(kind, key), partition_key=study_id)
        except CosmosResourceNotFoundError:
            return None
        return Record(doc["value"], doc["_etag"])

    async def put(self, kind: str, study_id: str, key: str, value: dict, *,
                  expected_revision: str | None) -> Record:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosHttpResponseError
        doc = {"id": self._id(kind, key), "study_id": study_id, "kind": kind,
               "key": key, "value": value}
        try:
            if expected_revision is None:
                result = await self.container.create_item(doc)
            else:
                result = await self.container.replace_item(
                    doc["id"], doc, etag=expected_revision,
                    match_condition=MatchConditions.IfNotModified)
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise Conflict("stale state revision") from exc
            raise
        return Record(result["value"], result["_etag"])

    async def list(self, kind: str, study_id: str) -> list[Record]:
        rows = self.container.query_items(
            query="SELECT * FROM c WHERE c.kind=@kind AND c.study_id=@study",
            parameters=[{"name": "@kind", "value": kind},
                        {"name": "@study", "value": study_id}], partition_key=study_id)
        return [Record(row["value"], row["_etag"]) async for row in rows]

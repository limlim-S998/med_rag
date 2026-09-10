"""Versioned Blob helpers. Source/citation retention lives in sources.py.

A serving-index retirement never deletes source or parsed evidence. Raw bytes
are content-addressed through BlobArtifacts; these helpers are for explicitly
versioned parsed artifacts. All writes are create-only and checked on retries.
"""

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob.aio import BlobServiceClient

from medw_core.persistence import Conflict

RAW = "raw"
PARSED = "parsed"
SNAPSHOTS = "snapshots"


def raw_path(study_id: str, doc_id: str, filename: str, *, source_revision: str) -> str:
    if not source_revision:
        raise ValueError("immutable source revision required")
    return f"{study_id}/{doc_id}/{source_revision}/{filename}"


def parsed_path(study_id: str, doc_id: str, artifact: str = "layout.json", *,
                source_revision: str, parser_version: str) -> str:
    if not source_revision or not parser_version:
        raise ValueError("source revision and parser version required")
    return f"{study_id}/{doc_id}/{source_revision}/{parser_version}/{artifact}"


async def put_json(bsc: BlobServiceClient, container: str, path: str, payload: bytes) -> None:
    blob = bsc.get_blob_client(container, path)
    try:
        await blob.upload_blob(payload, overwrite=False)
    except ResourceExistsError:
        if await get_bytes(bsc, container, path) != payload:
            raise Conflict("immutable parsed artifact already contains different bytes") from None


async def get_bytes(bsc: BlobServiceClient, container: str, path: str) -> bytes:
    return await (await bsc.get_blob_client(container, path).download_blob()).readall()

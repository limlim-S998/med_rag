"""Create-only Azure Blob artifact adapter."""

import hashlib

from medw_core.persistence import Conflict


class BlobArtifacts:
    """A container client with create-only content-addressed writes."""

    def __init__(self, container):
        self.container = container

    async def put(self, payload: bytes) -> str:
        from azure.core.exceptions import ResourceExistsError
        digest = hashlib.sha256(payload).hexdigest()
        blob = self.container.get_blob_client(digest)
        try:
            await blob.upload_blob(payload, overwrite=False)
        except ResourceExistsError:
            if await self.get(f"sha256:{digest}") != payload:
                raise Conflict("content-addressed artifact was corrupted") from None
        return f"sha256:{digest}"

    async def get(self, uri: str) -> bytes:
        digest = uri.removeprefix("sha256:")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid content-addressed artifact URI")
        payload = await (await self.container.get_blob_client(digest).download_blob()).readall()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise Conflict("artifact checksum mismatch")
        return payload


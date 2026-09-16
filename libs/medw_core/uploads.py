"""Registered, bounded upload capabilities; source evidence is captured at submission."""

from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import secrets
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from medw_core.persistence import Conflict, StateStore

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    doc_id: str | None = Field(default=None, min_length=1, max_length=64,
                               pattern=r"^[a-zA-Z0-9_-]+$")


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class UploadStorage(Protocol):
    async def issue(self, record: dict, token: str, public_base_url: str) -> str: ...
    async def read(self, record: dict) -> bytes: ...


class LocalUploadStorage:
    """Single-use signed-capability equivalent backed by durable files."""

    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, record: dict) -> pathlib.Path:
        # Never derive a disk path from a filename supplied by a caller.
        return self.root / record["upload_id"]

    async def issue(self, record: dict, token: str, public_base_url: str) -> str:
        return (f"{public_base_url.rstrip('/')}/studies/{quote(record['study_id'], safe='')}"
                f"/uploads/{record['upload_id']}?token={token}")

    async def write(self, record: dict, payload: bytes) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as output:
                temporary = pathlib.Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, self._path(record))
            except FileExistsError:
                if self._path(record).read_bytes() != payload:
                    raise Conflict("upload capability cannot replace existing bytes") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def read(self, record: dict) -> bytes:
        with self._path(record).open("rb") as stream:
            payload = stream.read(record["size_bytes"] + 1)
        return payload


class AzureUploadStorage:
    """User delegation SAS for exactly one staging blob, never an account key."""

    def __init__(self, service, container_name: str):
        self.service, self.container_name = service, container_name
        self.container = service.get_container_client(container_name)

    async def issue(self, record: dict, token: str, public_base_url: str) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        start = datetime.now(UTC) - timedelta(minutes=5)
        expiry = datetime.fromtimestamp(record["expires_at"], UTC)
        delegation = await self.service.get_user_delegation_key(start, expiry)
        sas = generate_blob_sas(
            account_name=self.service.account_name, container_name=self.container_name,
            blob_name=record["blob_name"], user_delegation_key=delegation,
            permission=BlobSasPermissions(create=True), start=start, expiry=expiry,
            protocol="https")
        return f"{self.container.get_blob_client(record['blob_name']).url}?{sas}"

    async def read(self, record: dict) -> bytes:
        blob = self.container.get_blob_client(record["blob_name"])
        from azure.core.exceptions import ResourceNotFoundError
        try:
            props = await blob.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise FileNotFoundError("registered file has not been uploaded") from exc
        if props.size != record["size_bytes"]:
            raise ValueError("uploaded file size differs from registration")
        # Capture one ETag: no upload overwrite can change bytes between check and read.
        from azure.core import MatchConditions
        stream = await blob.download_blob(etag=props.etag,
                                          match_condition=MatchConditions.IfNotModified)
        return await stream.readall()


class UploadService:
    def __init__(self, state: StateStore, storage: UploadStorage, *, ttl_seconds: int = 900,
                 max_bytes: int = MAX_UPLOAD_BYTES, clock=time.time):
        self.state, self.storage, self.clock = state, storage, clock
        self.ttl_seconds, self.max_bytes = ttl_seconds, max_bytes

    async def register(self, study_id: str, request: UploadRequest, *,
                       public_base_url: str) -> dict:
        if request.size_bytes > self.max_bytes:
            raise ValueError("file exceeds configured upload limit")
        upload_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        record = {**request.model_dump(), "study_id": study_id,
                  "doc_id": request.doc_id or uuid.uuid4().hex,
                  "upload_id": upload_id, "expires_at": self.clock() + self.ttl_seconds,
                  "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                  "blob_name": f"staging/{hashlib.sha256(study_id.encode()).hexdigest()}/{upload_id}"}
        url = await self.storage.issue(record, token, public_base_url)
        await self.state.put("upload", study_id, upload_id, record, expected_revision=None)
        return {"upload_id": upload_id, "doc_id": record["doc_id"], "upload_url": url,
                "expires_at": record["expires_at"], "method": "PUT",
                "headers": {"x-ms-blob-type": "BlockBlob"}, "max_bytes": self.max_bytes}

    async def record(self, study_id: str, upload_id: str, doc_id: str | None = None) -> dict:
        result = await self.state.get("upload", study_id, upload_id)
        if result is None or (doc_id is not None and result.value["doc_id"] != doc_id):
            raise KeyError("registered upload not found")
        if result.value["expires_at"] <= self.clock():
            raise PermissionError("upload has expired; register a new upload")
        return result.value

    @staticmethod
    def validate(record: dict, payload: bytes) -> None:
        if len(payload) != record["size_bytes"]:
            raise ValueError("uploaded file size differs from registration")
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ValueError("uploaded file checksum differs from registration")

    async def write_local(self, study_id: str, upload_id: str, token: str,
                          payload: bytes) -> None:
        record = await self.record(study_id, upload_id)
        if not hmac.compare_digest(record["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
            raise PermissionError("invalid upload capability")
        if not isinstance(self.storage, LocalUploadStorage):
            raise PermissionError("upload bytes directly to the issued Blob URL")
        self.validate(record, payload)
        await self.storage.write(record, payload)

    async def capture(self, study_id: str, doc_id: str, upload_id: str, evidence):
        record = await self.record(study_id, upload_id, doc_id)
        payload = await self.storage.read(record)
        self.validate(record, payload)
        return await evidence.ingest_source(study_id, doc_id, payload, record["filename"])

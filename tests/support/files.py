"""File-backed source and upload fixtures for offline ingestion tests."""

import hashlib
import os
import pathlib
import tempfile

from medw_core.persistence import Conflict


class FileArtifacts:
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                temporary = pathlib.Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise Conflict("content-addressed artifact was corrupted") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return f"sha256:{digest}"

    async def get(self, uri: str) -> bytes:
        digest = uri.removeprefix("sha256:")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid content-addressed artifact URI")
        payload = (self.root / digest).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise Conflict("artifact checksum mismatch")
        return payload


class FileUploadStorage:
    """Write fixture bytes directly; no server or signed local upload route."""

    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, record: dict) -> pathlib.Path:
        # Never derive a disk path from a filename supplied by a caller.
        return self.root / record["upload_id"]

    async def issue(self, record: dict) -> str:
        return f"https://uploads.invalid/{record['upload_id']}"

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


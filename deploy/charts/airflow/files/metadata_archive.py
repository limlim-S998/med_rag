"""Upload/restore a pg_dump archive; PostgreSQL owns dump and restore semantics."""
import argparse
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from azure.core import MatchConditions
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("upload", "download"))
    parser.add_argument("--file", type=Path, default=Path("/archive/metadata.dump"))
    parser.add_argument("--blob", help="Exact blob name from a backup's evidence record")
    args = parser.parse_args()
    with DefaultAzureCredential() as credential, BlobServiceClient(
        os.environ["BACKUP_ACCOUNT_URL"], credential=credential
    ) as service:
        container = service.get_container_client(os.environ.get("BACKUP_CONTAINER", "airflow-backups"))
        if args.operation == "upload":
            digest = checksum(args.file)
            name = f"metadata/{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.dump"
            blob = container.get_blob_client(name)
            with args.file.open("rb") as stream:
                blob.upload_blob(stream, overwrite=False, validate_content=True,
                                 metadata={"sha256": digest})
            properties = blob.get_blob_properties()
            if properties.size != args.file.stat().st_size or properties.metadata["sha256"] != digest:
                raise RuntimeError("Uploaded archive identity differs")
        else:
            if not args.blob or not args.blob.startswith("metadata/") or not args.blob.endswith(".dump"):
                parser.error("An exact metadata/*.dump backup name is required")
            name = args.blob
            blob = container.get_blob_client(name)
            properties = blob.get_blob_properties()
            with args.file.open("wb") as stream:
                blob.download_blob(etag=properties.etag,
                                   match_condition=MatchConditions.IfNotModified).readinto(stream)
            digest = checksum(args.file)
            if digest != properties.metadata.get("sha256") or args.file.stat().st_size != properties.size:
                raise RuntimeError("Downloaded archive failed checksum/size verification")
        print(json.dumps({"operation": args.operation, "blob": name, "sha256": digest,
                          "bytes": properties.size, "etag": properties.etag}))


if __name__ == "__main__":
    main()

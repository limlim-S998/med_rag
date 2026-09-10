"""Per-peer Qdrant snapshots, atomic Blob manifests, same-topology restoration.

Only a completed manifest is a backup. Each peer is write-locked for capture;
locks are restored on success/error/SIGTERM. A killed host can leave locks;
``unlock`` removes only locks explicitly marked by this tool. Restore requires
empty peers of the exact server version and count recorded in the manifest.
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx


def request(client, node, method, path, **kwargs):
    response = client.request(method, f"{node.rstrip('/')}{path}", **kwargs)
    response.raise_for_status()
    return response.json()["result"]


def identity(client, node, collection):
    """Content identity includes vectors/payload, not just point IDs/counts."""
    digest = hashlib.sha256()
    offset = None
    count = 0
    while True:
        result = request(client, node, "POST", f"/collections/{quote(collection, safe='')}/points/scroll",
                         json={"limit": 256, "offset": offset,
                               "with_payload": True, "with_vector": True})
        for point in result["points"]:
            digest.update(json.dumps(point, sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
        offset = result.get("next_page_offset")
        if offset is None:
            return {"count": count, "sha256": digest.hexdigest()}


@contextmanager
def frozen(client, nodes, backup_id):
    previous = []
    try:
        for node in nodes:
            old = request(client, node, "POST", "/locks", json={
                "write": True, "error_message": f"medw-backup:{backup_id}"})
            previous.append((node, old))
            if old.get("write"):
                raise RuntimeError(f"Peer {node} is already write-locked; refusing overlapping backup")
        yield
    finally:
        failures = []
        for node, old in reversed(previous):
            try:
                request(client, node, "POST", "/locks", json=old)
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{node}: {type(exc).__name__}")
        if failures:
            raise RuntimeError(f"Could not restore write locks; run unlock: {failures}")


def backup(client, nodes, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Backup directory must be empty; preserve the previous completed set")
    backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    versions = [client.get(node).raise_for_status().json()["version"] for node in nodes]
    if len(set(versions)) != 1:
        raise ValueError("Cannot snapshot mixed Qdrant server versions")
    manifest = {"format": 1, "id": backup_id, "created_at": datetime.now(UTC).isoformat(),
                "version": versions[0], "peer_count": len(nodes), "collections": [],
                "aliases": request(client, nodes[0], "GET", "/aliases")["aliases"]}
    with frozen(client, nodes, backup_id):
        collections = request(client, nodes[0], "GET", "/collections")["collections"]
        for item in sorted(collections, key=lambda c: c["name"]):
            name = item["name"]
            path = f"/collections/{quote(name, safe='')}"
            info = request(client, nodes[0], "GET", path)
            if info["status"] != "green":
                raise ValueError(f"Collection {name} is not green; refusing backup")
            record = {"name": name, "config": info["config"],
                      "identity": identity(client, nodes[0], name), "snapshots": []}
            for ordinal, node in enumerate(nodes):
                shards = []
                if len(nodes) > 1:
                    placement = request(client, node, "GET", path + "/cluster")
                    if any(shard["state"] != "Active" for shard in placement["local_shards"]):
                        raise ValueError(f"Collection {name} has non-active local replicas")
                    shards = [shard["shard_id"] for shard in placement["local_shards"]]
                snapshot = request(client, node, "POST", path + "/snapshots")["name"]
                filename = f"{len(manifest['collections'])}-{ordinal}.snapshot"
                try:
                    digest = hashlib.sha256()
                    with (directory / filename).open("wb") as file, client.stream(
                        "GET", f"{node}{path}/snapshots/{quote(snapshot, safe='')}"
                    ) as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes():
                            file.write(chunk)
                            digest.update(chunk)
                    record["snapshots"].append({"peer": ordinal, "file": filename,
                                                "sha256": digest.hexdigest(), "shards": shards})
                finally:
                    request(client, node, "DELETE", path + "/snapshots/" + quote(snapshot, safe=""))
            if identity(client, nodes[0], name) != record["identity"]:
                raise ValueError(f"Collection {name} changed during snapshot")
            manifest["collections"].append(record)
    # Rename is the local commit point. Upload uses the same manifest-last rule.
    (directory / "manifest.pending").write_text(json.dumps(manifest, indent=2) + "\n")
    (directory / "manifest.pending").replace(directory / "manifest.json")
    return manifest


def validate_archive(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["format"] != 1:
        raise ValueError("Unsupported snapshot manifest format")
    for collection in manifest["collections"]:
        if {s["peer"] for s in collection["snapshots"]} != set(range(manifest["peer_count"])):
            raise ValueError("Missing peer snapshot")
        for snapshot in collection["snapshots"]:
            filename = snapshot["file"]
            if Path(filename).name != filename:
                raise ValueError("Unsafe archive filename")
            with (directory / filename).open("rb") as file:
                actual = hashlib.file_digest(file, "sha256").hexdigest()
            if actual != snapshot["sha256"]:
                raise ValueError(f"Snapshot checksum mismatch: {filename}")
    return manifest


def restore(client, nodes, directory):
    manifest = validate_archive(directory)
    if len(nodes) != manifest["peer_count"]:
        raise ValueError("Restoration requires the original peer count")
    for node in nodes:
        if client.get(node).raise_for_status().json()["version"] != manifest["version"]:
            raise ValueError("Restoration requires the original Qdrant version")
        if request(client, node, "GET", "/collections")["collections"]:
            raise ValueError("Restoration target must contain no collections")
    for collection in manifest["collections"]:
        path = "/collections/" + quote(collection["name"], safe="")
        # Every peer snapshot was captured under one write freeze. no_sync
        # activates each restored local shard without invalidating a peer
        # already restored from the same consistent set (snapshot priority
        # would mark those other replicas Dead). Targets are verified empty.
        for snapshot in collection["snapshots"]:
            node = nodes[snapshot["peer"]]
            with (Path(directory) / snapshot["file"]).open("rb") as file:
                request(client, node, "POST", path + "/snapshots/upload?priority=no_sync",
                        files={"snapshot": (snapshot["file"], file, "application/octet-stream")})
        if len(nodes) > 1:
            # A fresh cluster allocates initial replicas independently of the
            # original peer IDs. Recovery adds snapshot-local shards; remove
            # only empty/extra placements not present in the captured topology,
            # after every intended replica is Active.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                placements = [request(client, node, "GET", path + "/cluster") for node in nodes]
                expected_active = all(
                    set(snapshot["shards"]) <= {
                        shard["shard_id"] for shard in placements[snapshot["peer"]]["local_shards"]
                        if shard["state"] == "Active"
                    } for snapshot in collection["snapshots"])
                if expected_active:
                    break
                time.sleep(1)
            else:
                raise ValueError(f"Restored replicas did not activate: {collection['name']}")
            for snapshot in collection["snapshots"]:
                placement = placements[snapshot["peer"]]
                for shard in placement["local_shards"]:
                    if shard["shard_id"] not in snapshot["shards"]:
                        request(client, nodes[0], "POST", path + "/cluster", json={
                            "drop_replica": {"peer_id": placement["peer_id"],
                                             "shard_id": shard["shard_id"]}})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                info = request(client, nodes[0], "GET", path)
                topology_matches = True
                if len(nodes) > 1:
                    for snapshot in collection["snapshots"]:
                        actual = request(client, nodes[snapshot["peer"]], "GET", path + "/cluster")
                        topology_matches = topology_matches and all(
                            shard["state"] == "Active" for shard in actual["local_shards"])
                        topology_matches = topology_matches and set(snapshot["shards"]) == {
                            shard["shard_id"] for shard in actual["local_shards"]}
                if topology_matches and info["status"] == "green" and identity(
                    client, nodes[0], collection["name"]
                ) == collection["identity"]:
                    break
            except httpx.HTTPError:
                # Snapshot recovery can temporarily leave remote shards
                # unavailable while their replicas transfer to the fresh peers.
                pass
            time.sleep(1)
        else:
            raise ValueError(f"Restored identity/status mismatch: {collection['name']}")
    aliases = [{"create_alias": alias} for alias in manifest.get("aliases", [])]
    if aliases:
        request(client, nodes[0], "POST", "/collections/aliases", json={"actions": aliases})
    return manifest


def blob_container():
    from azure.storage.blob import BlobServiceClient

    connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if connection:
        service = BlobServiceClient.from_connection_string(connection)
    else:
        from azure.identity import DefaultAzureCredential

        service = BlobServiceClient(os.environ["BACKUP_ACCOUNT_URL"],
                                    credential=DefaultAzureCredential())
    return service.get_container_client(os.environ.get("BACKUP_CONTAINER", "snapshots"))


def upload(container, directory, prefix="qdrant", retention_days=30):
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    manifest = validate_archive(directory)
    base = f"{prefix.strip('/')}/{manifest['id']}/"
    files = [s["file"] for c in manifest["collections"] for s in c["snapshots"]]
    for filename in [*files, "manifest.json"]:
        with (Path(directory) / filename).open("rb") as file:
            container.upload_blob(base + filename, file, overwrite=False)
    # Expire complete backup sets only, and only after a new complete upload.
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    entries = list(container.list_blobs(name_starts_with=prefix.strip("/") + "/"))
    committed = {blob.name.removesuffix("/manifest.json") for blob in entries
                 if blob.name.endswith("/manifest.json")}
    for blob in entries:
        if blob.name.endswith("/manifest.json") and blob.last_modified < cutoff:
            old_base = blob.name.removesuffix("manifest.json")
            old = json.loads(container.download_blob(blob.name).readall())
            for collection in old["collections"]:
                for snapshot in collection["snapshots"]:
                    container.delete_blob(old_base + snapshot["file"])
            container.delete_blob(blob.name)
        elif blob.name.rsplit("/", 1)[0] not in committed and blob.last_modified < cutoff:
            # A failed old upload has no manifest. It is never restored, and
            # cannot accumulate orphan snapshots forever.
            container.delete_blob(blob.name)
    return base


def download(container, key, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = container.download_blob(key.rstrip("/") + "/manifest.json").readall()
    manifest = json.loads(data)
    for collection in manifest["collections"]:
        for snapshot in collection["snapshots"]:
            filename = snapshot["file"]
            if Path(filename).name != filename:
                raise ValueError("Unsafe archive filename")
            with (directory / filename).open("wb") as file:
                container.download_blob(key.rstrip("/") + "/" + filename).readinto(file)
    (directory / "manifest.json").write_bytes(data)
    validate_archive(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore", "unlock"])
    parser.add_argument("--nodes", default=os.getenv("QDRANT_NODES", "http://localhost:6333"))
    parser.add_argument("--directory")
    parser.add_argument("--blob-key", help="Restore a completed Blob backup prefix")
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()
    nodes = args.nodes.split(",")
    def terminate(signum, frame):
        raise InterruptedError("Backup terminated; restoring write locks")
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory() as temporary, httpx.Client(
        headers={"api-key": os.environ["QDRANT_API_KEY"]} if os.getenv("QDRANT_API_KEY") else {},
        timeout=httpx.Timeout(300, connect=10),
    ) as client:
        directory = args.directory or temporary
        if args.operation == "unlock":
            for node in nodes:
                locks = request(client, node, "GET", "/locks")
                if locks.get("error_message", "").startswith("medw-backup:"):
                    request(client, node, "POST", "/locks", json={"write": False})
            return
        if args.operation == "backup":
            manifest = backup(client, nodes, directory)
            if args.upload:
                key = upload(blob_container(), directory, os.getenv("BACKUP_PREFIX", "qdrant"),
                             int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
                print(json.dumps({"event": "backup_uploaded", "key": key}))
        else:
            if args.blob_key:
                download(blob_container(), args.blob_key, directory)
            manifest = restore(client, nodes, directory)
        print(json.dumps({"event": args.operation + "_succeeded", "backup_id": manifest["id"],
                          "collections": len(manifest["collections"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"event": "backup_operation_failed", "error_type": type(exc).__name__,
                          "message": str(exc)}), file=sys.stderr)
        raise

#!/usr/bin/env python3
"""Disposable three-peer Docker proof: ACLs, node loss, snapshots and fresh restore.

Requires Docker plus the project's Python dev environment. No live cloud calls.
Run with --blob to also upload/download via disposable Azurite Blob storage.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup", ROOT /
    "deploy/charts/qdrant/files/qdrant_backup.py")
assert spec and spec.loader
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
IMAGE = "qdrant/qdrant@sha256:d774e7bb65744454984c6021637a0da89271f30df15e48601a9fafc926d26b1f"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def wait_for(fn, seconds=90):
    deadline = time.monotonic() + seconds
    error = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            error = exc
        time.sleep(1)
    raise AssertionError(f"Timed out: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blob", action="store_true")
    args = parser.parse_args()
    prefix = "medw-recovery-" + uuid.uuid4().hex[:8]
    names = []
    docker("network", "create", prefix)
    with tempfile.TemporaryDirectory(prefix=prefix) as temp, httpx.Client(
        headers={"api-key": "synthetic-admin"}, timeout=120
    ) as client:
        def cluster(label):
            nodes = []
            for i in range(3):
                name = f"{prefix}-{label}-{i}"
                names.append(name)
                options = ["run", "-d", "--name", name, "--network", prefix,
                           "-p", "127.0.0.1::6333", "--memory", "512m",
                           "-e", "QDRANT__CLUSTER__ENABLED=true",
                           "-e", "QDRANT__SERVICE__API_KEY=synthetic-admin",
                           "-e", "QDRANT__SERVICE__READ_ONLY_API_KEY=synthetic-reader",
                           IMAGE, "./qdrant", "--uri", f"http://{name}:6335"]
                if i:
                    options += ["--bootstrap", f"http://{prefix}-{label}-0:6335"]
                docker(*options)
                port = docker("port", name, "6333/tcp").rsplit(":", 1)[1]
                node = f"http://127.0.0.1:{port}"
                wait_for(lambda node=node: client.get(node + "/readyz").status_code == 200)
                nodes.append(node)
            wait_for(lambda: all(len(backup.request(client, node, "GET", "/cluster")["peers"])
                                 == 3 for node in nodes))
            return nodes
        try:
            nodes = cluster("source")
            collection = "/collections/synthetic"
            backup.request(client, nodes[0], "PUT", collection, json={
                "vectors": {"size": 4, "distance": "Dot"}, "shard_number": 3,
                "replication_factor": 2, "write_consistency_factor": 2})
            backup.request(client, nodes[0], "PUT", collection + "/points?wait=true", json={
                "points": [{"id": i, "vector": [float(i), 1.0, 0.0, 0.0],
                            "payload": {"source_revision": f"synthetic-{i}"}} for i in range(60)]})
            wait_for(lambda: backup.request(client, nodes[0], "GET", collection)["status"] == "green")
            placement = [backup.request(client, n, "GET", collection + "/cluster") for n in nodes]
            shards = {}
            for peer in placement:
                for shard in peer["local_shards"]:
                    assert shard["state"] == "Active", shard
                    shards[shard["shard_id"]] = shards.get(shard["shard_id"], 0) + 1
            assert shards == {0: 2, 1: 2, 2: 2}, shards
            assert client.get(nodes[0] + "/collections", headers={"api-key": "synthetic-reader"}).status_code == 200
            assert client.put(nodes[0] + collection + "/points", headers={"api-key": "synthetic-reader"},
                              json={"points": []}).status_code in {401, 403}
            assert client.get(nodes[0] + "/collections", headers={"api-key": "wrong"}).status_code in {401, 403}
            search = {"vector": [1.0, 0.0, 0.0, 0.0], "limit": 5, "with_payload": True}
            expected = backup.request(client, nodes[0], "POST", collection + "/points/search", json=search)
            docker("stop", names[0])
            found = backup.request(client, nodes[1], "POST", collection + "/points/search", json=search)
            assert found == expected
            docker("start", names[0])
            port = docker("port", names[0], "6333/tcp").rsplit(":", 1)[1]
            nodes[0] = f"http://127.0.0.1:{port}"
            wait_for(lambda: client.get(nodes[0] + "/readyz").status_code == 200)
            wait_for(lambda: backup.request(client, nodes[0], "GET", collection)["status"] == "green")
            archive = Path(temp) / "archive"
            manifest = backup.backup(client, nodes, archive)
            for node in nodes:
                assert not backup.request(client, node, "GET", "/locks")["write"]
            if args.blob:
                name = prefix + "-blob"
                names.append(name)
                docker("run", "-d", "--name", name, "--network", prefix,
                       "-p", "127.0.0.1::10000", "-e", "AZURITE_ACCOUNTS=medw:c3ludGhldGljLWJsb2Ita2V5",
                       "mcr.microsoft.com/azure-storage/azurite@sha256:647c63a91102a9d8e8000aab803436e1fc85fbb285e7ce830a82ee5d6661cf37",
                       "azurite-blob", "--blobHost", "0.0.0.0", "--skipApiVersionCheck")
                port = docker("port", name, "10000/tcp").rsplit(":", 1)[1]
                os.environ["AZURE_STORAGE_CONNECTION_STRING"] = (
                    "DefaultEndpointsProtocol=http;AccountName=medw;"
                    "AccountKey=c3ludGhldGljLWJsb2Ita2V5;"
                    f"BlobEndpoint=http://127.0.0.1:{port}/medw;")
                container = backup.blob_container()
                wait_for(lambda: (container.create_container() or True))
                key = backup.upload(container, archive)
                archive = Path(temp) / "download"
                backup.download(container, key, archive)
                assert len(list(container.list_blobs())) == 4
            restored = cluster("restored")
            backup.restore(client, restored, archive)
            restored_shards = {}
            for node in restored:
                state = backup.request(client, node, "GET", collection + "/cluster")
                for shard in state["local_shards"]:
                    assert shard["state"] == "Active", shard
                    key = shard["shard_id"]
                    restored_shards[key] = restored_shards.get(key, 0) + 1
            assert restored_shards == {0: 2, 1: 2, 2: 2}, restored_shards
            result = backup.request(client, restored[0], "POST", collection + "/points/search", json=search)
            assert result == expected
            print(json.dumps({"result": "passed", "peers": 3, "replication_factor": 2,
                              "points": manifest["collections"][0]["identity"]["count"],
                              "acl": "read-only rejects mutation", "node_loss": "reads match",
                              "restored": "content identity and sample search match",
                              "restored_shard_copies": restored_shards, "blob": args.blob}))
        finally:
            for name in reversed(names):
                subprocess.run(["docker", "rm", "-f", "-v", name], check=False, capture_output=True)
            docker("network", "rm", prefix)


if __name__ == "__main__":
    main()

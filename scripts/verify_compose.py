#!/usr/bin/env python3
"""Disposable all-service startup, dependency recovery and durable-volume proof."""
import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-tag", help="reuse locally built medw-SERVICE images")
    parser.add_argument("--output", default="/tmp/medw-compose-evidence.json")
    args = parser.parse_args()
    project = "medw-compose-proof-" + uuid.uuid4().hex[:8]
    environment = dict(os.environ)
    for service in (*SERVICES, "qdrant"):
        key = "INGESTION" if service == "ingestion-worker" else service.upper()
        environment[f"MEDW_{key}_PORT"] = "0"
    with tempfile.TemporaryDirectory(prefix="medw-compose-config-") as temporary:
        command = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml")]
        if args.image_tag:
            override = Path(temporary) / "images.json"
            override.write_text(json.dumps({"services": {
                service: {"image": f"medw-{service}:{args.image_tag}"} for service in SERVICES}}))
            command += ["-f", str(override)]
        command += ["--profile", "full"]

        def compose(*params):
            return run(*command, *params, env=environment)

        def container(service):
            return compose("ps", "-q", service)

        def port(service):
            details = json.loads(run("docker", "inspect", container(service)))[0]
            return details["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]

        def status(service, path):
            try:
                with urlopen(f"http://127.0.0.1:{port(service)}{path}", timeout=5) as response:
                    return response.status
            except HTTPError as error:
                return error.code
            except (URLError, TimeoutError, ConnectionError):
                return 0

        def wait_ready(expected):
            deadline = time.monotonic() + 90
            observed = {}
            while time.monotonic() < deadline:
                observed = {name: status(name, "/readyz") for name in expected}
                if observed == expected:
                    return
                time.sleep(1)
            raise AssertionError(f"readiness: expected {expected}, observed {observed}")

        try:
            compose("up", "-d", "--no-build" if args.image_tag else "--build")
            wait_ready(dict.fromkeys(SERVICES, 200))
            versions = {}
            for service in SERVICES:
                assert status(service, "/healthz") == 200
                with urlopen(f"http://127.0.0.1:{port(service)}/version", timeout=5) as response:
                    versions[service] = json.load(response)
                assert versions[service]["backend"] == "local"
                assert versions[service]["medical_handlers"] == "held-back"
            seed = """
import asyncio
from medw_core.persistence import SQLiteStateStore
from medw_core.local.platform import PersistentSessionStore
async def main():
    state = SQLiteStateStore('/data/platform.sqlite3')
    await PersistentSessionStore(state).put({'session_id':'restart-proof','user_id':'synthetic'})
    await state.close()
asyncio.run(main())
"""
            run("docker", "exec", container("gateway"), "python", "-c", seed)
            compose("restart", *SERVICES)
            wait_ready(dict.fromkeys(SERVICES, 200))
            readback = """
import asyncio
from medw_core.persistence import SQLiteStateStore
from medw_core.local.platform import PersistentSessionStore
async def main():
    state = SQLiteStateStore('/data/platform.sqlite3')
    assert await PersistentSessionStore(state).get('synthetic','restart-proof') == {
        'session_id':'restart-proof','user_id':'synthetic'}
    await state.close()
asyncio.run(main())
"""
            run("docker", "exec", container("gateway"), "python", "-c", readback)
            compose("stop", "qdrant")
            wait_ready({"retrieval": 503, "gateway": 503})
            assert status("retrieval", "/healthz") == status("gateway", "/healthz") == 200
            compose("start", "qdrant")
            wait_ready(dict.fromkeys(SERVICES, 200))
            report = {"result": "passed", "image_tag": args.image_tag, "versions": versions,
                      "checks": ["all five services live and ready with synthetic local dependencies",
                                 "persisted session survives application container restart",
                                 "Qdrant outage propagates readiness503 while liveness200",
                                 "dependency recovery restores readiness without app restart"]}
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"result": report["result"], "checks": report["checks"]}, indent=2))
        finally:
            # Only this randomly named disposable project owns these volumes.
            compose("down", "-v", "--remove-orphans")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check packaged Azure startup, telemetry imports and honest process probes.

Storage endpoints are syntactically valid but unreachable inside a networkless
container. SDK construction must succeed; dependency checks must report outages.
The smoke run cannot send telemetry or credentials to any external service.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import subprocess
import time
import uuid


def smoke_airflow(image: str) -> None:
    """A disposable PostgreSQL plus the real DAG; no application deployment."""
    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    postgres_image = yaml.safe_load((root / "deploy/charts/airflow/values.yaml").read_text())["postgres"]["image"]
    name = "medw-airflow-check-" + uuid.uuid4().hex[:10]
    database, runtime = name + "-db", name + "-dag"
    environment = {**os.environ, "POSTGRES_PASSWORD": secrets.token_urlsafe(24)}
    environment["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = (
        "postgresql://airflow:" + environment["POSTGRES_PASSWORD"] + "@airflow-db:5432/airflow")
    subprocess.run(["docker", "network", "create", "--internal", name], check=True, capture_output=True)
    try:
        subprocess.run(["docker", "run", "--detach", "--name", database, "--network", name,
                        "--network-alias", "airflow-db", "--env", "POSTGRES_USER=airflow",
                        "--env", "POSTGRES_DB=airflow", "--env", "POSTGRES_PASSWORD", postgres_image],
                       env=environment, check=True, capture_output=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", database, "pg_isready", "-U", "airflow"],
                                    check=False, capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("temporary Airflow metadata database did not start")
        subprocess.run(["docker", "run", "--name", runtime, "--network", name,
                        "--env", "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "--mount",
                        f"type=bind,source={root / 'tests/support/airflow_smoke.py'},target=/tmp/airflow_smoke.py,readonly",
                        image, "python", "/tmp/airflow_smoke.py"], env=environment, check=True, timeout=180)
    finally:
        for container in (runtime, database):
            subprocess.run(["docker", "rm", "--force", "--volumes", container], check=False, capture_output=True)
        subprocess.run(["docker", "network", "rm", name], check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--service", required=True)
    args = parser.parse_args()
    if args.service == "airflow":
        smoke_airflow(args.image)
        return
    name = "medw-smoke-" + uuid.uuid4().hex[:12]
    source = subprocess.run(["docker", "image", "inspect", args.image, "--format",
                             '{{index .Config.Labels "org.opencontainers.image.revision"}}'],
                            check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["docker", "run", "--detach", "--name", name, "--network", "none",
                    "--env", "MEDW_ENV=test",
                    "--env", "MEDW_COSMOS_ENDPOINT=https://cosmos.invalid",
                    "--env", "MEDW_BLOB_ACCOUNT_URL=https://storage.invalid",
                    "--env", "MEDW_SEARCH_ENDPOINT=https://search.invalid",
                    "--env", "MEDW_SQL_SERVER=sql.invalid",
                    "--env", "MEDW_READINESS_CACHE_SECONDS=0",
                    "--env", f"MEDW_SERVICE_NAME={args.service}",
                    "--env", f"MEDW_IMAGE_SHA={source}",
                    "--env", ("MEDW_APPINSIGHTS_CONNECTION_STRING="
                    "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                    "IngestionEndpoint=http://127.0.0.1:9;LiveEndpoint=http://127.0.0.1:9"),
                    "--env", "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL=true",
                    args.image], check=True, capture_output=True)
    probe = """import urllib.request, urllib.error, sys
try:
    r=urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1], timeout=5)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
"""
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", name, "python", "-c", probe, "/healthz"],
                                    capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip() == "200":
                break
            running = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                                     capture_output=True, text=True, check=True)
            if running.stdout.strip() != "true":
                raise RuntimeError("service exited before becoming live")
            time.sleep(0.5)
        else:
            raise RuntimeError("liveness deadline exceeded")
        result = subprocess.run(["docker", "exec", name, "python", "-c", probe, "/readyz"],
                                capture_output=True, text=True, check=True)
        # All other services depend on infrastructure absent from this isolated
        # container. Full readiness is checked by the application verification.
        expected = "200" if args.service == "reranker" else "503"
        if source == "unversioned":
            expected = "503"
        allowed = {expected}
        if result.stdout.strip() not in allowed:
            raise RuntimeError(f"unexpected readiness: {result.stdout.strip()}")
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=True)
        if "dependency initialization failed" in logs.stdout + logs.stderr:
            raise RuntimeError("dependency initialization failed inside a live process")
        subprocess.run(["docker", "exec", name, "pip", "check"], check=True)
        print(f"{args.service}: health=200 ready={result.stdout.strip()}")
    except Exception:
        subprocess.run(["docker", "logs", "--tail", "50", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


if __name__ == "__main__":
    main()

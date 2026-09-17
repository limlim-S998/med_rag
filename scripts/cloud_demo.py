#!/usr/bin/env python3
"""Run normal application API clients in AKS, controlled from Azure Cloud Shell.

The operator needs Python's standard library and kubectl. The client runs in the
selected ingestion image, with its own workload identity and no store grants.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import ssl
import subprocess
import time
import uuid
from urllib.parse import quote

PREFIX = "MEDW_EVENT "


def emit(stage, **detail):
    print(PREFIX + json.dumps({"stage": stage, **detail}), flush=True)


def wait_for(check, seconds=600):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(3)
    raise TimeoutError("cloud demonstration did not finish within its deadline")


def client(processing):
    import httpx
    from azure.identity import WorkloadIdentityCredential

    if __package__:
        from .demo_run import workflow
    else:
        from demo_run import workflow

    path = pathlib.Path("/work/inputs.json")
    wait_for(path.exists, seconds=120)
    inputs = json.loads(path.read_text())
    base, study, section = (os.environ[name] for name in (
        "MEDW_DEMO_BASE_URL", "MEDW_DEMO_STUDY", "MEDW_DEMO_SECTION"))
    ca = "/etc/medw/ca.pem"
    trust = ssl.create_default_context(cafile=ca)
    scope = "api://" + os.environ["MEDW_AUTH_AUDIENCE"] + "/.default"
    with WorkloadIdentityCredential() as credential, httpx.Client(base_url=base, verify=trust, timeout=60) as http:
        def request(method, url, **kwargs):
            response = http.request(method, url, headers={
                "Authorization": "Bearer " + credential.get_token(scope).token}, **kwargs)
            if not response.is_success:
                raise RuntimeError(f"public API returned HTTP {response.status_code}")
            return response.json()

        actor = request("GET", "/me")
        version = request("GET", "/version")
        emit("connected", actor=actor, release=version, url=base)
        prefix = "/studies/" + quote(study, safe="")
        denied = http.post(prefix + "/search", json={"query": "authentication check"})
        if denied.status_code != 401:
            raise AssertionError("unauthenticated request was not rejected")
        evidence = []
        for index, item in enumerate(inputs):
            folder = pathlib.Path("/work") / str(index)
            folder.mkdir()
            source = folder / pathlib.Path(item["filename"]).name
            source.write_bytes(base64.b64decode(item["data"], validate=True))
            result = workflow(base, source, study, section, credential.get_token(scope).token, ca,
                              processing=processing, submit_only=processing == "nightly",
                              on_progress=lambda stage, detail: emit(stage, **detail))
            if processing == "nightly" and result["state"] != "scheduled":
                raise AssertionError("nightly submission was processed before batch admission")
            evidence.append(result)
        if processing == "nightly":
            emit("awaiting_batch", jobs=[item["job_id"] for item in evidence])
            previous = {}

            def finished():
                jobs = [request("GET", prefix + "/jobs/" + item["job_id"]) for item in evidence]
                for job in jobs:
                    if previous.get(job["id"]) != job["state"]:
                        emit("job", job_id=job["id"], state=job["state"], batch_id=job.get("batch_id"))
                        previous[job["id"]] = job["state"]
                    if job["state"] in {"failed", "superseded"}:
                        raise AssertionError("nightly document did not complete")
                return jobs if all(job["state"] == "done" for job in jobs) else None

            jobs = wait_for(finished)
            ids = {job.get("batch_id") for job in jobs}
            if None in ids or len(ids) != 1:
                raise AssertionError("documents did not join one batch")
            batch = request("GET", prefix + "/batches/" + next(iter(ids)))
            if batch["status"] != "completed":
                raise AssertionError("batch did not complete")
            for item, job in zip(evidence, jobs, strict=True):
                found = request("POST", prefix + "/search", json={
                    "query": "sha256:" + item["input"]["sha256"], "top_k": 8})
                if not any(hit["citation"]["source_revision"] == item["source_revision"] for hit in found["hits"]):
                    raise AssertionError("batch input missing from retrieval")
                item.update(state=job["state"], batch_id=job["batch_id"], job_checkpoints=job["checkpoints"])
                item["checks"].update(ingestion_done=True, retrieved_uploaded_source=True)
            emit("batch_completed", batch=batch)
        emit("complete", evidence=evidence, actor=actor, unauthenticated_rejected=True)


def job_manifest(name, image, processing):
    env = [{"name": variable, "valueFrom": {"configMapKeyRef": {
        "name": "medw-demo-client", "key": key}}} for variable, key in (
            ("MEDW_DEMO_BASE_URL", "base-url"), ("MEDW_DEMO_STUDY", "study-id"),
            ("MEDW_DEMO_SECTION", "section-path"), ("MEDW_AUTH_AUDIENCE", "audience"))]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "medw"},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 3600,
                "template": {"metadata": {"labels": {
                    "medw-component": "demo-client", "azure.workload.identity/use": "true"}},
                    "spec": {"restartPolicy": "Never", "serviceAccountName": "demo-client",
                        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
                        "containers": [{"name": "client", "image": image,
                            "command": ["python", "/app/operations/cloud_demo.py", "client", "--processing", processing],
                            "env": [*env, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                          "limits": {"cpu": "500m", "memory": "256Mi"}},
                            "volumeMounts": [{"name": "work", "mountPath": "/work"},
                                             {"name": "trust", "mountPath": "/etc/medw", "readOnly": True},
                                             {"name": "tmp", "mountPath": "/tmp"}]}],
                        "volumes": [{"name": "work", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                                    {"name": "trust", "configMap": {"name": "medw-demo-client",
                                        "items": [{"key": "ca.pem", "path": "ca.pem"}]}}]}}}}


def run_cloud(args):
    kube = ["kubectl", *(["--kubeconfig", str(args.kubeconfig)] if args.kubeconfig else []), "-n", "medw"]

    def command(*parts, payload=None):
        result = subprocess.run([*kube, *parts], input=payload, capture_output=True, check=False)
        if result.returncode:
            # Kubernetes errors can echo request bodies; do not print file data.
            raise RuntimeError(f"kubectl {parts[0]} failed with exit code {result.returncode}")
        return result.stdout.decode()

    name = "walkthrough-" + uuid.uuid4().hex[:10]
    if args.file:
        inputs = [{"filename": path.name, "data": base64.b64encode(path.read_bytes()).decode()} for path in args.file]
    else:
        inputs = [{"filename": f"{name}-{n}.txt", "data": base64.b64encode(
            f"Synthetic document {n} for {name}. The study enrolled {12 + n} fictional participants.\n".encode()).decode()}
                  for n in range(2 if args.processing == "nightly" else 1)]
    if not 1 <= len(inputs) <= 10 or any(not 0 < len(base64.b64decode(i["data"])) <= 5 * 1024 * 1024 for i in inputs):
        raise ValueError("Supply 1-10 files, each between 1 byte and 5 MiB")
    release = json.loads(command("get", "helmrelease", "ingestion-worker", "-o", "json"))
    artifact = release["spec"]["values"]["image"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]):
        raise ValueError("A selected immutable image is required")
    report = {"schema_version": 1, "environment": "azure", "job": name, "processing": args.processing,
              "client_image": artifact, "events": [], "passed": False}
    output = args.output or pathlib.Path("data/azure") / (name + ".json")
    created, seen, triggered = False, 0, False
    try:
        command("create", "-f", "-", payload=json.dumps(job_manifest(
            name, artifact["repository"] + "@" + artifact["digest"], args.processing)).encode())
        created = True

        def ready():
            pods = json.loads(command("get", "pods", "-l", "job-name=" + name, "-o", "json"))["items"]
            return next((p["metadata"]["name"] for p in pods if any(
                c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", []))), None)

        pod = wait_for(ready, seconds=180)
        report["pod"] = pod
        writer = "import pathlib,sys; p=pathlib.Path('/work/inputs.tmp'); p.write_bytes(sys.stdin.buffer.read()); p.rename('/work/inputs.json')"
        command("exec", "-i", pod, "-c", "client", "--", "python", "-c", writer,
                payload=json.dumps(inputs).encode())

        def completed():
            nonlocal seen, triggered
            lines = command("logs", pod, "-c", "client").splitlines()
            for line in lines[seen:]:
                if not line.startswith(PREFIX):
                    continue
                event = json.loads(line[len(PREFIX):])
                # Stream text for the audience; retain IDs/checksums in evidence.
                if event["stage"] == "draft_delta":
                    print(event["text"], end="", flush=True)
                else:
                    print(json.dumps(event), flush=True)
                    report["events"].append(event)
                if event["stage"] == "awaiting_batch" and not triggered:
                    command("exec", "statefulset/airflow-scheduler", "-c", "scheduler", "--",
                            "airflow", "dags", "trigger", "ingest_study", "--run-id", name)
                    triggered = True
                    report["dag_run_id"] = name
                    print("Triggered the installed nightly DAG:", name, flush=True)
            seen = len(lines)
            status = json.loads(command("get", "job", name, "-o", "json"))["status"]
            if status.get("failed"):
                raise RuntimeError("Azure client job failed; inspect the retained events")
            return status.get("succeeded")

        wait_for(completed, seconds=720)
        if not any(e["stage"] == "complete" for e in report["events"]):
            raise AssertionError("client exited without durable completion evidence")
        if triggered:
            batches = [e["batch"] for e in report["events"] if e["stage"] == "batch_completed"]
            if len(batches) != 1 or batches[0]["run_id"] != name:
                raise AssertionError("documents were not processed by the requested Airflow run")
            query = """import json,sys
from sqlalchemy import select
from airflow.models.dagrun import DagRun
from airflow.utils.session import create_session
with create_session() as s:
 r=s.scalar(select(DagRun).where(DagRun.dag_id=='ingest_study',DagRun.run_id==sys.argv[1]))
 print(json.dumps({'state':str(r.state) if r else None}))
"""

            def dag_finished():
                raw = command("exec", "statefulset/airflow-scheduler", "-c", "scheduler", "--",
                              "python", "-c", query, name)
                state = json.loads(raw.strip().splitlines()[-1])["state"]
                if state == "failed":
                    raise AssertionError("Airflow DAG failed")
                return state if state == "success" else None

            report["dag_state"] = wait_for(dag_finished, seconds=180)
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        if created:
            try:
                command("delete", "job", name, "--wait=true", "--timeout=60s")
                report["client_job_removed"] = True
            except RuntimeError:
                report["client_job_removed"] = False
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print("Evidence:", output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "client"))
    parser.add_argument("--processing", choices=("immediate", "nightly"), default="immediate")
    parser.add_argument("--file", type=pathlib.Path, action="append")
    parser.add_argument("--kubeconfig", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if args.action == "client":
        try:
            client(args.processing)
        except Exception as exc:  # noqa: BLE001 - redact SDK errors at the client process boundary
            code = re.search(r"HTTP (\d{3})|AADSTS(\d+)", str(exc))
            emit("error", error_type=type(exc).__name__,
                 http_status=code.group(1) if code else None, identity_code=code.group(2) if code else None)
            return 1
    else:
        run_cloud(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

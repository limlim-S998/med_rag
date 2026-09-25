"""Operational acceptance against the ordinary Azure installation.

The report records observed checks individually. Missing access or an unobserved
transition is never converted into a pass. Temporary restore resources are
removed in finally blocks; the source Qdrant volume is never a restore target.
"""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import importlib
import json
import os
import pathlib
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from string import Template
from urllib.parse import quote

import httpx
import yaml

SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")
INFLIGHT_METRIC = "medw_inflight_requests"
CORRELATION_DIMENSION = "medw.correlation_id"


class NotVerified(RuntimeError):
    """The prerequisite exists, but the requested evidence was not observed."""


def command(args, *, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        # Commands can contain administrative data. Exclude CLI stdout/stderr
        # and token-bearing request arguments from the machine-readable report.
        raise RuntimeError(f"{args[0]} {args[1]} exited {result.returncode}")
    return result.stdout.strip()


def await_value(check, *, timeout=180, interval=3, label="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        print(f"Waiting for {label}", flush=True)
        time.sleep(interval)
    raise NotVerified(f"{label} not observed within {timeout}s")


def correlation_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"correlation_id", "trace_id"} and isinstance(item, str):
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item):
                    yield item
            else:
                yield from correlation_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from correlation_ids(item)


def safe_failure(exc):
    # HTTP and SDK exception messages often contain URLs with SAS credentials.
    if isinstance(exc, NotVerified):
        return str(exc)
    return type(exc).__name__


def complete_trace_rows(table):
    """All service roles must share one workflow ID, regardless of KQL column order."""
    columns = [column["name"] for column in table.get("columns", [])]
    if not {"cid", "roles", "records"} <= set(columns):
        return []
    matched = []
    for values in table.get("rows", []):
        row = dict(zip(columns, values, strict=True))
        roles = json.loads(row["roles"]) if isinstance(row["roles"], str) else row["roles"]
        if set(SERVICES) <= set(roles):
            matched.append({"correlation_id": row["cid"], "roles": roles, "records": row["records"]})
    return matched


class Acceptance:
    def __init__(self, resources, kubeconfig, directory, study, section, *,
                 file=None, token=None, token_provider=None, workflow=None):
        self.resources = resources
        self.kubeconfig = pathlib.Path(kubeconfig)
        self.directory = pathlib.Path(directory)
        self.study, self.section = study, section
        self.file = pathlib.Path(file) if file else self.directory / "acceptance-source.txt"
        self.token = token or os.getenv("MEDW_API_TOKEN", "")
        self.token_provider = token_provider
        self.workflow = workflow
        self.base = "https://" + resources["hostname"]
        self.ca = None
        self.tls = ssl.create_default_context()
        self.report = {"timestamp": dt.datetime.now(dt.UTC).isoformat(), "environment": "azure",
                       "resources": resources, "checks": {}, "passed": False}
        self.completed_workflows = []
        self.run_id = uuid.uuid4().hex[:10]

    def kube(self, *arguments, payload=None, json_result=False):
        result = subprocess.run(["kubectl", "--kubeconfig", str(self.kubeconfig), *arguments],
                                input=payload, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"kubectl {arguments[0]} exited {result.returncode}")
        return json.loads(result.stdout) if json_result else result.stdout.strip()

    def apply(self, *manifests):
        self.kube("apply", "-f", "-", payload=json.dumps({
            "apiVersion": "v1", "kind": "List", "items": list(manifests)}))

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "acceptance.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.report, stream, indent=2)

    def check(self, name: str, action: Callable):
        print(f"Azure acceptance: {name}", flush=True)
        started = time.monotonic()
        try:
            self.refresh_token()
            result = action()
            self.report["checks"][name] = {"status": "passed", "evidence": result}
            return result
        except Exception as exc:  # noqa: BLE001 - preserve evidence for every operational failure
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            self.report["checks"][name] = {
                "status": "not_verified" if isinstance(exc, NotVerified) else "failed",
                "reason": safe_failure(exc),
                "location": f"{pathlib.Path(frame.filename).name}:{frame.lineno}"}
            return None
        finally:
            self.report["checks"][name]["elapsed_seconds"] = round(time.monotonic() - started, 2)
            self.save()

    def refresh_token(self):
        if self.token_provider:
            self.token = self.token_provider()
            if not self.token:
                raise RuntimeError("API token refresh returned no credential")

    @contextlib.contextmanager
    def forward(self, namespace, resource, remote_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([
            "kubectl", "--kubeconfig", str(self.kubeconfig), "-n", namespace,
            "port-forward", resource, f"{port}:{remote_port}", "--address", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def available():
                if process.poll() is not None:
                    raise RuntimeError("port-forward exited")
                with socket.socket() as test:
                    return test.connect_ex(("127.0.0.1", port)) == 0
            await_value(available, timeout=30, interval=0.5, label=resource + " port-forward")
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def pods(self, service):
        return self.kube("-n", "medw", "get", "pods", "-l", "app=" + service,
                           "-o", "json", json_result=True)["items"]

    def ready_releases(self, expected_bundle=None):
        def settled():
            releases = self.kube("-n", "medw", "get", "helmreleases", "-o", "json", json_result=True)
            names = {item["metadata"]["name"] for item in releases["items"]}
            if not {*SERVICES, "airflow", "qdrant"} <= names:
                return None
            for release in releases["items"]:
                if not any(c["type"] == "Ready" and c["status"] == "True" and
                           c.get("observedGeneration") == release["metadata"]["generation"]
                           for c in release.get("status", {}).get("conditions", [])):
                    return None
                if expected_bundle and release["metadata"]["name"] in (*SERVICES, "airflow"):
                    bundle = release["spec"].get("values", {}).get("config", {}).get("release_bundle_sha")
                    if bundle != expected_bundle:
                        return None
            return releases
        releases = await_value(settled, timeout=900, interval=5,
                               label="all Helm releases ready at their selected revisions")
        evidence = {}
        for service in SERVICES:
            pods = self.pods(service)
            ready = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
                     and any(c["type"] == "Ready" and c["status"] == "True"
                             for c in pod.get("status", {}).get("conditions", []))]
            if not ready:
                raise AssertionError(service + " has no ready pod")
            versions = []
            for pod in ready:
                script = ("import json,urllib.request; "
                          "assert urllib.request.urlopen('http://127.0.0.1:8000/readyz').status==200; "
                          "print(urllib.request.urlopen('http://127.0.0.1:8000/version').read().decode())")
                version = json.loads(self.kube("-n", "medw", "exec", pod["metadata"]["name"],
                                               "--", "python", "-c", script))
                versions.append({"pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                                 "version": version, "images": [c.get("imageID") for c in
                                     pod["status"]["containerStatuses"]]})
            release = next(item for item in releases["items"] if item["metadata"]["name"] == service)
            selected = release["spec"]["values"]
            expected = selected["image"]["digest"]
            for pod in versions:
                if (pod["version"]["image_digest"] != expected
                        or pod["version"]["image_sha"] != selected["image"]["sourceSha"]
                        or not pod["images"]
                        or not all(actual and expected in actual for actual in pod["images"])):
                    raise AssertionError(service + " running digest differs from selected release")
            evidence[service] = {"pods": versions, "helm_history": release.get("status", {}).get("history", []),
                                 "source_sha": selected["image"]["sourceSha"],
                                 "bundle_sha": selected["config"]["release_bundle_sha"]}
        sources = self.kube("-n", "flux-system", "get", "gitrepositories,kustomizations",
                              "-o", "json", json_result=True)
        # Store controller status, not complete manifests that might gain secrets.
        return {"services": evidence, "flux": [{"name": item["metadata"]["name"],
                "kind": item["kind"], "status": item.get("status", {})} for item in sources["items"]]}

    def public_access(self):
        statuses = {}
        with httpx.Client(verify=self.tls, timeout=30) as client:
            for path, expected in (("/version", 200), ("/metrics", 404), ("/readyz", 404),
                                   (f"/studies/{quote(self.study)}/jobs/missing", 401)):
                response = client.get(self.base + path)
                if response.status_code != expected:
                    raise AssertionError("public route status mismatch")
                statuses[path] = response.status_code
        return statuses

    def application(self, *, on_submitted=None):
        # A release build can take longer than the previous access token's life.
        self.refresh_token()
        if not self.token:
            raise NotVerified("MEDW_API_TOKEN is required for real authenticated API verification")
        if self.workflow is None:
            module = importlib.import_module("scripts.api")
            self.workflow = module.workflow
        if not self.file.exists():
            self.file.parent.mkdir(parents=True, exist_ok=True)
            # A text artifact is data, not another project documentation file.
            self.file.write_text("Operational acceptance source. Patients: 12.\n" * 100)
        kwargs = {"base_url": self.base, "file": self.file,
                  "study": self.study, "section": self.section,
                  "token": self.token, "ca_file": self.ca, "doc_id": "acceptance-" + self.run_id}
        if on_submitted is not None:
            kwargs["on_submitted"] = on_submitted
        result = self.workflow(**kwargs)
        if result.get("passed") is False or not result.get("checks") or not all(result["checks"].values()):
            raise AssertionError("application workflow did not prove all required checks")
        self.completed_workflows.append(result)
        return result

    def airflow_batch(self):
        """Use the installed DAG, not a verifier that dispatches jobs itself."""
        self.refresh_token()
        if not self.token:
            raise NotVerified("batch submission requires an authenticated writer")
        module = importlib.import_module("scripts.api")
        run_id = "acceptance-" + self.run_id
        submitted = []
        with tempfile.TemporaryDirectory(prefix="medw-nightly-") as directory:
            for ordinal in range(2):
                path = pathlib.Path(directory) / f"nightly-{ordinal}.txt"
                path.write_text(f"Nightly batch source {run_id}, document {ordinal}: 12 patients.\n")
                evidence = module.workflow(
                    self.base, path, self.study, self.section,
                    self.token, self.ca, doc_id=f"nightly-{run_id}-{ordinal}",
                    processing="nightly", submit_only=True)
                if evidence["state"] != "scheduled":
                    raise AssertionError("nightly input was not deferred")
                submitted.append(evidence)
        prefix = self.base + "/studies/" + quote(self.study, safe="")
        with httpx.Client(verify=self.tls, timeout=30,
                          headers={"Authorization": "Bearer " + self.token}) as client:
            # A manual trigger exercises the exact installed nightly DAG without
            # keeping the paid cluster alive until 2am. The cron is checked below.
            self.kube("-n", "medw", "exec", "statefulset/airflow-scheduler", "-c", "scheduler", "--",
                        "airflow", "dags", "trigger", "ingest_study", "--run-id", run_id)

            def finished():
                jobs = []
                for submission in submitted:
                    response = client.get(prefix + "/jobs/" + submission["job_id"])
                    response.raise_for_status()
                    job = response.json()
                    if job["state"] in {"failed", "superseded"}:
                        raise AssertionError("nightly document did not complete")
                    jobs.append(job)
                return jobs if all(job["state"] == "done" for job in jobs) else None

            jobs = await_value(finished, timeout=600, interval=5, label="Airflow batch document completion")
            batch_ids = {job.get("batch_id") for job in jobs}
            if None in batch_ids or len(batch_ids) != 1:
                raise AssertionError("documents did not join the same Airflow batch")
            batch_id = next(iter(batch_ids))
            response = client.get(prefix + "/batches/" + batch_id)
            response.raise_for_status()
            batch = response.json()
            if batch["run_id"] != run_id or batch["status"] != "completed":
                raise AssertionError("the requested DAG did not complete the batch")
            for submission, job in zip(submitted, jobs, strict=True):
                response = client.post(prefix + "/search", json={
                    "query": "sha256:" + submission["input"]["sha256"], "top_k": 8})
                response.raise_for_status()
                if not any(hit["citation"]["source_revision"] == job["source_revision"]
                           for hit in response.json()["hits"]):
                    raise AssertionError("batch source is missing from retrieval")
        script = """import json,sys
from sqlalchemy import select
from airflow.models.dagrun import DagRun
from airflow.utils.session import create_session
with create_session() as session:
    row=session.scalar(select(DagRun).where(DagRun.dag_id=='ingest_study',DagRun.run_id==sys.argv[1]))
    print(json.dumps({'state':str(row.state) if row else None}))
"""

        def dag_finished():
            raw = self.kube("-n", "medw", "exec", "statefulset/airflow-scheduler", "-c", "scheduler",
                              "--", "python", "-c", script, run_id)
            outcome = json.loads(raw.strip().splitlines()[-1])
            if outcome["state"] == "failed":
                raise AssertionError("Airflow reported a failed DAG run")
            return outcome if outcome["state"] == "success" else None

        dag_state = await_value(dag_finished, timeout=180, interval=5, label="Airflow DAG success")
        release = self.kube("-n", "medw", "get", "helmrelease", "airflow", "-o", "json", json_result=True)
        values = release["spec"]["values"]
        configured = {v["name"]: v["value"] for v in values["airflow"]["env"]}
        pods = self.kube("-n", "medw", "get", "pods", "-l", "medw-component=airflow,component=scheduler",
                           "-o", "json", json_result=True)["items"]
        running = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        if not running or any(not c.get("imageID") or values["image"]["digest"] not in c["imageID"]
                              for p in running for c in p["status"]["containerStatuses"]):
            raise AssertionError("Airflow scheduler image differs from selected release")
        return {"run_id": run_id, "dag_state": dag_state, "batch_id": batch_id, "counts": batch["counts"],
                "jobs": [{"id": job["id"], "source_revision": job["source_revision"],
                          "correlation_id": job["correlation_id"]} for job in jobs],
                "inputs": [item["input"] for item in submitted], "image": values["image"],
                "scheduler_pods": [p["metadata"]["uid"] for p in running],
                "schedule": configured["MEDW_BATCH_SCHEDULE"], "timezone": configured["MEDW_BATCH_TIMEZONE"]}

    def worker_recovery(self):
        if not self.completed_workflows:
            raise NotVerified("recovery requires a previously published study generation")
        observed: dict = {}
        # A batch may have published since the last immediate workflow. Compare
        # interrupted staging with the currently selected manifest, not that
        # earlier workflow's historical generation.
        baseline = self.publication_state(self.completed_workflows[-1]["job_id"])["active"]
        if not baseline:
            raise NotVerified("recovery requires an active study generation")
        secret = self.kube("-n", "medw", "get", "secret", "qdrant-auth", "-o", "json", json_result=True)
        key = base64.b64decode(secret["data"]["api-key"]).decode()
        original_file = self.file
        # A different revision forces actual index publication instead of the
        # already-published generation's idempotent fast path.
        with tempfile.TemporaryDirectory(prefix="medw-recovery-") as temporary, \
                self.forward("medw", "svc/qdrant", 6333) as address, \
                httpx.Client(base_url=address, headers={"api-key": key}, timeout=15) as qdrant, \
                httpx.Client(verify=self.tls, timeout=30,
                             headers={"Authorization": "Bearer " + self.token}) as client:
            recovery_file = pathlib.Path(temporary) / original_file.name
            recovery_file.write_bytes(original_file.read_bytes() + ("\nRecovery " + self.run_id).encode())
            def restart(job, restore_quota):
                path = self.base + f"/studies/{quote(self.study)}/jobs/{job['id']}"
                def interrupted_publication():
                    current = client.get(path).raise_for_status().json()
                    if current["state"] in {"done", "failed"}:
                        raise NotVerified("publication was not interrupted before the terminal job state")
                    return current if (current["state"] == "indexing" and
                        current["checkpoints"].get("embedding") and current.get("attempts", 0) > 0) else None
                current = await_value(interrupted_publication, timeout=25, interval=0.2,
                                      label="durable failed publication with retained checkpoints")
                state = self.publication_state(job["id"])
                if not state["plan"] or state["active"] != baseline:
                    raise AssertionError("interrupted staging changed the active generation")
                pods = self.pods("ingestion-worker")
                observed.update(job_id=job["id"], state_before=current["state"],
                                checkpoints_before=current["checkpoints"],
                                attempts_before=current["attempts"], publication=state["plan"],
                                prior_active_generation=state["active"],
                                old_uids=[pod["metadata"]["uid"] for pod in pods])
                # Upserts still fail under the quota while the old process
                # exits. Restore immediately afterwards, before replica startup,
                # so the bounded retry budget is not consumed by deployment waits.
                for pod in pods:
                    self.kube("-n", "medw", "delete", "pod", pod["metadata"]["name"],
                                "--grace-period=5", "--wait=true", "--timeout=20s")
                restore_quota()
                self.kube("-n", "medw", "rollout", "status", "deployment/ingestion-worker", "--timeout=5m")
                observed["new_uids"] = [pod["metadata"]["uid"] for pod in self.pods("ingestion-worker")]
                if set(observed["old_uids"]) & set(observed["new_uids"]):
                    raise AssertionError("worker UID did not change")
            with self.quota_outage(qdrant) as (restore_quota, quota_evidence):
                try:
                    self.file = recovery_file
                    result = self.application(on_submitted=lambda job: restart(job, restore_quota))
                    if not observed:
                        raise NotVerified("workflow did not invoke its submission callback")
                    for stage, artifact in observed["checkpoints_before"].items():
                        if result["job_checkpoints"].get(stage) != artifact:
                            raise AssertionError("restart rewrote a durable checkpoint")
                    if result["index_generation"]["generation_id"] != observed["publication"]["generation_id"]:
                        raise AssertionError("retry changed the persisted generation identity")
                    return {"recovery": observed, "quota_outage": quota_evidence, "workflow": result}
                finally:
                    self.file = original_file

    @contextlib.contextmanager
    def quota_outage(self, qdrant):
        """Induce a measured resource outage; this is not a snapshot write freeze."""
        status = qdrant.get("/quotas").raise_for_status().json()["result"]
        previous = status["config"]
        if previous.get("enabled"):
            raise NotVerified("preserving an already enabled Qdrant resource quota")
        candidates = (("resident_memory_percent", "max_resident_memory_percent"),
                      ("disk_usage_percent", "max_disk_usage_percent"))
        field = next((limit for usage, limit in candidates if (status["usage"].get(usage) or 0) > 1), None)
        if field is None:
            raise NotVerified("Qdrant usage is too low to induce a measured quota outage")
        limited = {**previous, "enabled": True, field: 1, "release_margin_percent": 0}
        active = True
        probe = "/collections/medw-quota-check-" + self.run_id
        created = False

        def restore():
            nonlocal active
            if not active:
                return
            current = qdrant.get("/quotas").raise_for_status().json()["result"]["config"]
            if current != previous:
                if current != limited:
                    raise RuntimeError("Qdrant quota changed concurrently; refusing to overwrite")
                qdrant.put("/quotas?wait=true", json=previous).raise_for_status()
                if qdrant.get("/quotas").raise_for_status().json()["result"]["config"] != previous:
                    raise RuntimeError("original Qdrant quota was not restored")
            active = False

        try:
            qdrant.put("/quotas?wait=true", json=limited).raise_for_status()
            creation = qdrant.put(probe, json={"vectors": {"size": 2, "distance": "Dot"}})
            creation.raise_for_status()
            created = True
            rejected = qdrant.put(probe + "/points?wait=true", json={"points": [{
                "id": 1, "vector": [1.0, 0.0], "payload": {"purpose": "operational quota check"}}]})
            if rejected.status_code != 507:
                raise NotVerified("measured Qdrant quota did not reject an actual upsert with HTTP507")
            qdrant.get("/readyz").raise_for_status()
            qdrant.get("/collections").raise_for_status()
            yield restore, {"usage_before": status["usage"], "limited_resource": field,
                            "upsert_status": rejected.status_code, "collection_creation_status": creation.status_code,
                            "readiness_and_reads_available": True}
        finally:
            try:
                restore()
            finally:
                if created:
                    qdrant.delete(probe).raise_for_status()

    def publication_state(self, job_id):
        """Read persisted state through the worker's existing workload identity."""
        script = """
import asyncio,json,sys
from azure.identity.aio import DefaultAzureCredential
from azure.cosmos.aio import CosmosClient
from medw_core.settings import get_settings
from medw_core.cosmos_state import CosmosStateStore
async def read():
    request=json.load(sys.stdin); s=get_settings()
    async with DefaultAzureCredential() as identity, CosmosClient(s.cosmos_endpoint,credential=identity) as cosmos:
        state=CosmosStateStore(cosmos.get_database_client(s.cosmos_database).get_container_client(s.cosmos_state_container))
        plan=await state.get('job_publication',request['study'],request['job'])
        active=await state.get('active_index',request['study'],'active')
        print(json.dumps({'plan':plan.value['generation'] if plan else None,
                          'active':active.value['generation']['generation_id'] if active else None}))
asyncio.run(read())
"""
        return json.loads(self.kube("-n", "medw", "exec", "-i", "deployment/ingestion-worker", "--", "python", "-c", script,
            payload=json.dumps({"study": self.study, "job": job_id})))

    def negative_access(self):
        if not self.token:
            raise NotVerified("authenticated rejection checks require the configured user token")
        evidence = {}
        prefix = self.base + "/studies/" + quote(self.study, safe="")
        denied = self.base + "/studies/DENIED-" + self.run_id + "/documents"

        def expect(name, response, expected):
            evidence[name] = response.status_code
            if response.status_code != expected:
                raise AssertionError(name + " was not rejected as expected")

        with httpx.Client(verify=self.tls, timeout=30) as client:
            forged = {"X-User-Oid": "forged-administrator", "X-Study-Id": self.study}
            expect("forged_headers_without_jwt", client.get(prefix + "/documents", headers=forged), 401)
            client.headers["Authorization"] = "Bearer " + self.token
            expect("cross_study", client.get(denied), 403)
            expect("forged_headers_cross_study", client.get(denied, headers=forged), 403)
            path = prefix + "/sections/" + quote(self.section, safe="") + "/draft"
            expect("forged_actor_body", client.post(path, json={"query": "verification", "user_oid": "forged"}), 422)
            payload = b"registered-content"
            upload = client.post(prefix + "/documents:upload-url", json={
                "filename": "checksum-rejection.bin", "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest()}).raise_for_status().json()
            # SAS upload carries only its blob capability, never the API JWT.
            with httpx.Client(timeout=30) as blob:
                blob.put(upload["upload_url"], content=b"x" * len(payload),
                         headers=upload["headers"]).raise_for_status()
            idempotency = "checksum-rejection-" + self.run_id
            expect("checksum_mismatch", client.post(prefix + "/documents/" + upload["doc_id"] + "/ingest",
                json={"upload_id": upload["upload_id"], "idempotency_key": idempotency}), 409)
            expect("rejected_checksum_has_no_job", client.get(prefix + "/jobs/" +
                   hashlib.sha256(idempotency.encode()).hexdigest()), 404)
        return evidence


    def persistence(self):
        before = self.kube("-n", "medw", "get", "pvc", "storage-qdrant-0", "-o", "json", json_result=True)
        old = self.pods("qdrant")
        self.kube("-n", "medw", "rollout", "restart", "statefulset/qdrant")
        self.kube("-n", "medw", "rollout", "status", "statefulset/qdrant", "--timeout=5m")
        after = self.kube("-n", "medw", "get", "pvc", "storage-qdrant-0", "-o", "json", json_result=True)
        if before["metadata"]["uid"] != after["metadata"]["uid"] or before["spec"]["volumeName"] != after["spec"]["volumeName"]:
            raise AssertionError("Qdrant persistent volume binding changed")
        current = self.pods("qdrant")
        if {p["metadata"]["uid"] for p in old} & {p["metadata"]["uid"] for p in current}:
            raise AssertionError("Qdrant pod did not restart")
        if not self.completed_workflows:
            raise NotVerified("indexing workflow must run before persistence verification")
        # Search retained indexed bytes before uploading any new source.
        statuses = []
        workflow = self.completed_workflows[-1]
        with httpx.Client(verify=self.tls, timeout=30, headers={"Authorization": "Bearer " + self.token}) as client:
            def recovered_search():
                response = client.post(self.base + f"/studies/{quote(self.study)}/search",
                                       json={"query": "sha256:" + workflow["input"]["sha256"], "top_k": 5})
                statuses.append(response.status_code)
                # Qdrant readiness precedes downstream probes, endpoint updates
                # and NGINX recovery. A bounded wait must observe a real success.
                if response.status_code in (502, 503):
                    return None
                return response.raise_for_status().json()
            result = await_value(recovered_search, timeout=90, interval=3,
                                 label="public retrieval after Qdrant replacement")
            hits = result["hits"]
            expected_source = workflow["source_revision"]
            if not any(hit.get("citation", {}).get("source_revision") == expected_source for hit in hits):
                raise AssertionError("uploaded source was not searchable after restart")
        return {"pvc_uid": after["metadata"]["uid"], "volume": after["spec"]["volumeName"],
                "pod_uid": current[0]["metadata"]["uid"], "search_statuses": statuses,
                "retained_citations": [h["citation"] for h in hits]}

    def _job(self, name, template):
        metadata = {"name": name, "namespace": "medw", "labels": {"medw-verification": self.run_id}}
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata,
               "spec": {**template["spec"]["jobTemplate"]["spec"], "activeDeadlineSeconds": 300,
                        "backoffLimit": 0}}
        self.apply(job)
        self.kube("-n", "medw", "wait", "job/" + name, "--for=condition=Complete", "--timeout=5m")
        output = self.kube("-n", "medw", "logs", "job/" + name)
        events = []
        for line in output.splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
        return events

    def backup_restore(self):
        if not self.completed_workflows:
            raise NotVerified("a completed ingestion is required before snapshot verification")
        name = "medw-restore-" + self.run_id
        backup_name, restore_name = name + "-backup", name + "-restore"
        template = self.kube("-n", "medw", "get", "cronjob", "qdrant-backup", "-o", "json", json_result=True)
        source = self.kube("-n", "medw", "get", "statefulset", "qdrant", "-o", "json", json_result=True)
        if source["spec"]["replicas"] != 1:
            raise NotVerified("this bounded acceptance restores the configured single-node topology")
        try:
            events = self._job(backup_name, template)
            uploaded = next((e for e in events if e.get("event") == "backup_uploaded"), None)
            if uploaded is None or not re.fullmatch(r"[A-Za-z0-9_/-]+", uploaded.get("key", "")):
                raise AssertionError("backup job did not report a committed Blob manifest")
            image = source["spec"]["template"]["spec"]["containers"][0]["image"]
            self.apply(
                {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "medw",
                    "labels": {"app": name, "medw-verification": self.run_id}}, "spec": {
                    "automountServiceAccountToken": False, "containers": [{"name": "qdrant", "image": image,
                        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                      "limits": {"cpu": "1", "memory": "512Mi"}},
                        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 6333}},
                        "volumeMounts": [{"name": "restore", "mountPath": "/qdrant/storage"}]}],
                    "volumes": [{"name": "restore", "emptyDir": {}}]}},
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": "medw"},
                 "spec": {"selector": {"app": name}, "ports": [{"port": 6333}]}},
                {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": name, "namespace": "medw"}, "spec": {
                    "podSelector": {"matchLabels": {"app": "qdrant-backup"}}, "policyTypes": ["Egress"],
                    "egress": [{"to": [{"podSelector": {"matchLabels": {"app": name}}}],
                                "ports": [{"protocol": "TCP", "port": 6333}]}]}},
                {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": name + "-target", "namespace": "medw"}, "spec": {
                    "podSelector": {"matchLabels": {"app": name}}, "policyTypes": ["Ingress"],
                    "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "qdrant-backup"}}}],
                                 "ports": [{"protocol": "TCP", "port": 6333}]}]}})
            self.kube("-n", "medw", "wait", "pod/" + name, "--for=condition=Ready", "--timeout=3m")
            container = template["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            container["command"] = ["python", "/backup/qdrant_backup.py", "restore", "--blob-key", uploaded["key"]]
            for env in container["env"]:
                if env["name"] == "QDRANT_NODES":
                    env["value"] = "http://" + name + ":6333"
            restored = self._job(restore_name, template)
            if not any(event.get("event") == "restore_succeeded" and event.get("collections", 0) > 0
                       for event in restored):
                raise AssertionError("restored content identity verification did not finish")
            return {"blob_manifest_key": uploaded["key"].rstrip("/") + "/manifest.json", "restore_target": name,
                    "target_storage": "isolated emptyDir", "restore": restored}
        finally:
            self.kube("-n", "medw", "delete", "job", backup_name, restore_name, "--ignore-not-found", "--wait=false")
            self.kube("-n", "medw", "delete", "pod,service,networkpolicy", name, "--ignore-not-found", "--wait=false")
            self.kube("-n", "medw", "delete", "networkpolicy", name + "-target", "--ignore-not-found")

    def airflow_backup_restore(self):
        """Restore the real metadata dump into an isolated volume, never airflow-db."""
        name = "airflow-restore-" + self.run_id
        backup_name = name + "-backup"
        cron = self.kube("-n", "medw", "get", "cronjob", "airflow-backup", "-o", "json", json_result=True)
        try:
            events = self._job(backup_name, cron)
            uploaded = next((event for event in events if event.get("operation") == "upload"), None)
            if (not uploaded or not re.fullmatch(r"metadata/[A-Za-z0-9-]+\.dump", uploaded.get("blob", ""))
                    or not re.fullmatch(r"[a-f0-9]{64}", uploaded.get("sha256", ""))):
                raise AssertionError("Airflow backup did not report its committed archive identity")
            container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            template = pathlib.Path(__file__).resolve().parents[2] / "deploy/operations/airflow-restore.yaml"
            manifests = list(yaml.safe_load_all(Template(template.read_text()).substitute(
                INGESTION_IMAGE=container["image"], BLOB_URL=self.resources["blob_url"],
                BACKUP_BLOB=uploaded["blob"])))
            for manifest in manifests:
                manifest["metadata"]["name"] = name
            job = next(m for m in manifests if m["kind"] == "Job")
            volume = next(v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "restore")
            volume["persistentVolumeClaim"]["claimName"] = name
            self.apply(*manifests)
            self.kube("-n", "medw", "wait", "job/" + name, "--for=condition=Complete", "--timeout=10m")
            download = self.kube("-n", "medw", "logs", "job/" + name, "-c", "download")
            downloaded = json.loads(download.strip().splitlines()[-1])
            if any(downloaded.get(key) != uploaded[key] for key in ("blob", "sha256", "bytes")):
                raise AssertionError("Restored archive differs from the backup")
            output = self.kube("-n", "medw", "logs", "job/" + name, "-c", "restore")
            restored = None
            for line in output.splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("event") == "airflow_restore_verified":
                        restored = event
            if not restored or not restored.get("schema") or restored.get("dag_runs", 0) < 1:
                raise AssertionError("Restored Airflow schema and completed run history were not observed")
            return {"archive": uploaded, "restore": restored, "target_pvc": name,
                    "target_storage": "separate temporary PVC", "live_database_modified": False}
        finally:
            # Remove restore pods before releasing their separate volume.
            self.kube("-n", "medw", "delete", "job", name, backup_name,
                      "--ignore-not-found", "--wait=true", "--timeout=90s")
            self.kube("-n", "medw", "delete", "pvc", name, "--ignore-not-found", "--wait=false")

    def monitoring_and_scaling(self):
        if not self.token or not self.completed_workflows:
            raise NotVerified("a completed authenticated workflow is required before load verification")
        self.ready_releases()
        def replicas():
            value = self.kube("-n", "medw", "get", "deployment", "generation", "-o", "json", json_result=True)
            return value["status"].get("readyReplicas", 0)
        await_value(lambda: replicas() == 1, timeout=240, label="generation baseline of one replica")
        stop = threading.Event()
        counts = {"completed": 0, "failed": 0}
        failures = {}
        lock = threading.Lock()

        def load():
            # Each draft has its own connection. NGINX replaces workers when
            # endpoints change; don't reuse an idle connection from that worker.
            # Failed requests are still counted, and drafts are never retried.
            with httpx.Client(verify=self.tls, timeout=60,
                              limits=httpx.Limits(max_keepalive_connections=0),
                              headers={"Authorization": "Bearer " + self.token}) as client:
                while not stop.is_set():
                    try:
                        response = client.post(self.base + f"/studies/{quote(self.study)}/sections/"
                            + quote(self.section) + "/draft",
                            json={"query": "Operational acceptance", "top_k": 1, "max_tokens": 128})
                        response.raise_for_status()
                        if not any(json.loads(line).get("type") == "complete" for line in response.text.splitlines()):
                            raise ValueError("uncommitted draft")
                        with lock:
                            counts["completed"] += 1
                    except (httpx.HTTPError, ValueError, OSError) as exc:
                        with lock:
                            counts["failed"] += 1
                            reason = ("http_" + str(exc.response.status_code)
                                      if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__)
                            if isinstance(exc, httpx.RemoteProtocolError) and "without sending a response" in str(exc):
                                reason = "connection_closed_before_response"
                            failures[reason] = failures.get(reason, 0) + 1
                    # Bound offered load against the shared 400 RU/s database.
                    # This verifies scaling, not maximum sustainable throughput.
                    stop.wait(0.1)
        def query(expression):
            # Use the authenticated Kubernetes service proxy for each read;
            # a long-lived local port-forward can disappear during the load.
            path = ("/api/v1/namespaces/monitoring/services/http:prometheus-operated:9090"
                    "/proxy/api/v1/query?query=" + quote(expression, safe=""))
            result = self.kube("--request-timeout=30s", "get", "--raw", path, json_result=True)
            if result.get("status") != "success":
                raise AssertionError("Prometheus query failed")
            return result["data"]["result"]

        def metric_services():
            series = query(f"sum by(app) ({INFLIGHT_METRIC})")
            observed = {row["metric"].get("app") for row in series}
            return observed if set(SERVICES) <= observed else None
        observed = await_value(metric_services, timeout=120, interval=3,
                               label="Prometheus samples from every ready service")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(load) for _ in range(2)]
            try:
                await_value(lambda: replicas() == 2, timeout=180, label="KEDA scale-up under real drafting")
                gauge = query(f'sum({INFLIGHT_METRIC}{{app="generation"}})')
            finally:
                stop.set()
                for future in futures:
                    future.result(timeout=70)
                self.report["load_observation"] = {"clients": 2, "top_k": 1,
                    "pause_seconds": 0.1, "connection_reuse": False,
                    "real_drafts": counts, "failures": failures}
                self.save()
        await_value(lambda: replicas() == 1, timeout=240, label="KEDA scale-down after drafting")
        if not counts["completed"] or counts["failed"]:
            raise AssertionError("load included unsuccessful or uncommitted requests")
        return {"metric_services": sorted(observed), "replicas": [1, 2, 1],
                "real_drafts": counts, "inflight_sample": gauge}

    def traces(self):
        ids = sorted(set(correlation_ids(self.completed_workflows)))
        if not ids:
            raise NotVerified("workflow evidence contains no correlation IDs")
        resource = json.loads(command(["az", "resource", "show", "--ids", self.resources["insights"], "-o", "json"]))
        app_id = resource["properties"]["AppId"]
        credential = json.loads(command(["az", "account", "get-access-token", "--resource",
                                         "https://api.applicationinsights.io", "-o", "json"]))["accessToken"]
        query = ('union requests, dependencies, traces | where timestamp > ago(1h) '
                 f'| extend cid=tostring(customDimensions["{CORRELATION_DIMENSION}"]) '
                 '| where cid in (' + ",".join(json.dumps(value) for value in ids) + ') '
                 '| summarize roles=make_set(cloud_RoleName), records=count() by cid')
        with httpx.Client(timeout=30, headers={"Authorization": "Bearer " + credential}) as client:
            def observed():
                result = client.get(f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                                    params={"query": query}).raise_for_status().json()
                return complete_trace_rows(result.get("tables", [{}])[0]) or None
            rows = await_value(observed, timeout=300, interval=15, label="correlated Application Insights spans")
        return {"correlation_ids": ids, "rows": rows}

    def blob_content(self):
        if not self.completed_workflows:
            raise NotVerified("a completed upload is required for Blob readback")
        result = self.completed_workflows[-1]
        document = result["document"]
        digest = document["artifact_uri"].removeprefix("sha256:")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AssertionError("unsafe source artifact identity")
        script = ("import hashlib,json,os; from azure.identity import DefaultAzureCredential; "
                  "from azure.storage.blob import BlobServiceClient; "
                  "client=BlobServiceClient(os.environ['MEDW_BLOB_ACCOUNT_URL'],credential=DefaultAzureCredential()); "
                  "data=client.get_blob_client(os.environ['MEDW_BLOB_CONTAINER']," + repr(digest) + ").download_blob().readall(); "
                  "print(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)}))")
        actual = json.loads(self.kube("-n", "medw", "exec", "deployment/ingestion-worker", "--", "python", "-c", script))
        if actual != result["input"]:
            raise AssertionError("direct Blob bytes differ from the uploaded source")
        return {"artifact_uri": document["artifact_uri"], **actual}

    def audit_rows(self, events):
        # The operator is the configured SQL administrator. Its short-lived
        # token travels on exec stdin, never through arguments or a journal.
        token = json.loads(command(["az", "account", "get-access-token", "--resource",
                                    "https://database.windows.net/", "-o", "json"]))["accessToken"]
        script = """import asyncio,json,struct,sys
from sqlalchemy import text
from medw_core.settings import Settings
from medw_core.sql import engine
body=json.load(sys.stdin)
raw=body['token'].encode('utf-16-le')
async def read():
    database=engine(Settings(),token=struct.pack('<I',len(raw))+raw)
    try:
        params={'e'+str(i):value for i,value in enumerate(body['events'])}
        statement='SELECT event_id,release_bundle_sha,image_sha,prompt_bundle_sha,output_sha256 FROM audit.generation_event WHERE event_id IN ('+','.join(':'+name for name in params)+')'
        async with database.connect() as conn:
            rows=(await conn.execute(text(statement),params)).mappings().all()
        print(json.dumps([{key:str(value) for key,value in row.items()} for row in rows]))
    finally:
        await database.dispose()
asyncio.run(read())
"""
        output = self.kube("-n", "medw", "exec", "-i", "deployment/generation", "--", "python", "-c", script,
                             payload=json.dumps({"token": token, "events": events}))
        rows = json.loads(output)
        for row in rows:
            row["event_id"] = str(uuid.UUID(row["event_id"]))
        if len(rows) != len(events):
            raise AssertionError("a completed draft is missing its SQL audit row")
        return sorted(rows, key=lambda row: row["event_id"])

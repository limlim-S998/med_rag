#!/usr/bin/env python3
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
import uuid
from collections.abc import Callable
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
    def __init__(self, deployment, *, file=None, token=None, workflow=None):
        self.d = deployment
        self.file = pathlib.Path(file) if file else self.d.directory / "acceptance-source.txt"
        self.token = token or os.getenv("MEDW_DEMO_TOKEN", "")
        self.workflow = workflow
        self.base = "https://" + self.d.state["hostname"]
        self.ca = self.d.directory / "tls/server.crt"
        self.tls = ssl.create_default_context(cafile=str(self.ca))
        self.report = {"timestamp": dt.datetime.now(dt.UTC).isoformat(), "environment": "azure",
                       "resources": self.d.state["resources"], "checks": {}, "passed": False}
        self.completed_workflows = []
        self.run_id = uuid.uuid4().hex[:10]

    def save(self):
        self.d.directory.mkdir(parents=True, exist_ok=True)
        path = self.d.directory / "acceptance.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.report, stream, indent=2)

    def check(self, name: str, action: Callable):
        print(f"Azure acceptance: {name}", flush=True)
        started = time.monotonic()
        try:
            result = action()
            self.report["checks"][name] = {"status": "passed", "evidence": result}
            return result
        except Exception as exc:  # noqa: BLE001 - preserve evidence for every operational failure
            self.report["checks"][name] = {
                "status": "not_verified" if isinstance(exc, NotVerified) else "failed",
                "reason": safe_failure(exc)}
            return None
        finally:
            self.report["checks"][name]["elapsed_seconds"] = round(time.monotonic() - started, 2)
            self.save()

    @contextlib.contextmanager
    def forward(self, namespace, resource, remote_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([
            "kubectl", "--kubeconfig", str(self.d.directory / "kubeconfig"), "-n", namespace,
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
        return self.d.kube("-n", "medw", "get", "pods", "-l", "app=" + service,
                           "-o", "json", json_result=True)["items"]

    def ready_releases(self):
        self.d.kube("-n", "medw", "wait", "helmrelease", "--all",
                    "--for=condition=Ready", "--timeout=15m")
        releases = self.d.kube("-n", "medw", "get", "helmreleases", "-o", "json", json_result=True)
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
                version = json.loads(self.d.kube("-n", "medw", "exec", pod["metadata"]["name"],
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
        sources = self.d.kube("-n", "flux-system", "get", "gitrepositories,kustomizations",
                              "-o", "json", json_result=True)
        # Store controller status, not complete manifests that might gain secrets.
        return {"services": evidence, "flux": [{"name": item["metadata"]["name"],
                "kind": item["kind"], "status": item.get("status", {})} for item in sources["items"]]}

    def public_access(self):
        statuses = {}
        with httpx.Client(verify=self.tls, timeout=30) as client:
            for path, expected in (("/version", 200), ("/metrics", 404), ("/readyz", 404),
                                   (f"/studies/{quote(self.d.config['study_id'])}/jobs/missing", 401)):
                response = client.get(self.base + path)
                if response.status_code != expected:
                    raise AssertionError("public route status mismatch")
                statuses[path] = response.status_code
        return statuses

    def application(self, *, on_submitted=None):
        if not self.token:
            raise NotVerified("MEDW_DEMO_TOKEN is required for real authenticated API verification")
        if self.workflow is None:
            module = importlib.import_module("scripts.demo_run")
            self.workflow = module.workflow
        if not self.file.exists():
            self.file.parent.mkdir(parents=True, exist_ok=True)
            # A text artifact is data, not another project documentation file.
            self.file.write_text("Operational acceptance source. Patients: 12.\n" * 100)
        kwargs = {"base_url": self.base, "file": self.file,
                  "study": self.d.config["study_id"], "section": self.d.config["section_path"],
                  "token": self.token, "ca_file": str(self.ca), "doc_id": "medw-operational-acceptance"}
        if on_submitted is not None:
            kwargs["on_submitted"] = on_submitted
        result = self.workflow(**kwargs)
        if result.get("passed") is False or not result.get("checks") or not all(result["checks"].values()):
            raise AssertionError("application workflow did not prove all required checks")
        self.completed_workflows.append(result)
        return result

    def worker_recovery(self):
        if not self.completed_workflows:
            raise NotVerified("recovery requires a previously published study generation")
        observed: dict = {}
        baseline = self.completed_workflows[-1]["index_generation"]["generation_id"]
        secret = self.d.kube("-n", "medw", "get", "secret", "qdrant-auth", "-o", "json", json_result=True)
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
                path = self.base + f"/studies/{quote(self.d.config['study_id'])}/jobs/{job['id']}"
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
                    self.d.kube("-n", "medw", "delete", "pod", pod["metadata"]["name"],
                                "--grace-period=5", "--wait=true", "--timeout=20s")
                restore_quota()
                self.d.kube("-n", "medw", "rollout", "status", "deployment/ingestion-worker", "--timeout=5m")
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
        return json.loads(self.d.kube("-n", "medw", "exec", "-i", "deployment/ingestion-worker", "--", "python", "-c", script,
            payload=json.dumps({"study": self.d.config["study_id"], "job": job_id})))

    def negative_access(self):
        if not self.token:
            raise NotVerified("authenticated rejection checks require the configured user token")
        evidence = {}
        prefix = self.base + "/studies/" + quote(self.d.config["study_id"], safe="")
        denied = self.base + "/studies/DENIED-" + self.run_id + "/documents"

        def expect(name, response, expected):
            evidence[name] = response.status_code
            if response.status_code != expected:
                raise AssertionError(name + " was not rejected as expected")

        with httpx.Client(verify=self.tls, timeout=30) as client:
            forged = {"X-User-Oid": "forged-administrator", "X-Study-Id": self.d.config["study_id"]}
            expect("forged_headers_without_jwt", client.get(prefix + "/documents", headers=forged), 401)
            client.headers["Authorization"] = "Bearer " + self.token
            expect("cross_study", client.get(denied), 403)
            expect("forged_headers_cross_study", client.get(denied, headers=forged), 403)
            path = prefix + "/sections/" + quote(self.d.config["section_path"], safe="") + "/draft"
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

    def expired_upload(self):
        """Change ordinary gateway TTL through Flux and exercise a real issued SAS."""
        if not self.token:
            raise NotVerified("upload expiry checks require the configured user token")
        original: dict = {}
        ttl = 5  # Allows the real user-delegation-key request to complete first.
        path = "deploy/flux/dev/environment-values.yaml"

        def configure(work, restore=False):
            target = work / path
            documents = list(yaml.safe_load_all(target.read_text()))
            item = next(row for row in documents if row["metadata"]["name"] == "gateway")
            config = item["spec"]["values"].setdefault("config", {})
            if restore:
                if config.get("upload_ttl_seconds") != ttl:
                    raise RuntimeError("gateway upload TTL changed concurrently; refusing to overwrite")
                if original["present"]:
                    config["upload_ttl_seconds"] = original["value"]
                else:
                    config.pop("upload_ttl_seconds", None)
            else:
                original.update(present="upload_ttl_seconds" in config,
                                value=config.get("upload_ttl_seconds"))
                if original["value"] == ttl:
                    raise NotVerified("gateway already has the verification TTL; preserve its configuration")
                config["upload_ttl_seconds"] = ttl
            target.write_text(yaml.safe_dump_all(documents, sort_keys=False))
            return [target]

        def settled(expected):
            release = self.d.kube("-n", "medw", "get", "helmrelease", "gateway", "-o", "json", json_result=True)
            actual = release["spec"].get("values", {}).get("config", {}).get("upload_ttl_seconds")
            return actual == expected and any(c["type"] == "Ready" and c["status"] == "True" and
                c.get("observedGeneration") == release["metadata"]["generation"]
                for c in release.get("status", {}).get("conditions", []))

        revision = self.publish_change(configure, "verify: bound registered upload lifetime [skip ci]")
        try:
            await_value(lambda: settled(ttl), timeout=600, interval=10, label="gateway short upload lifetime")
            prefix = self.base + "/studies/" + quote(self.d.config["study_id"], safe="")
            with httpx.Client(verify=self.tls, timeout=30,
                              headers={"Authorization": "Bearer " + self.token}) as client:
                payload = b"expired-registration"
                upload = client.post(prefix + "/documents:upload-url", json={
                    "filename": "expired.bin", "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest()}).raise_for_status().json()
                # Expires-at is registered state, not a forged or edited SAS.
                remaining = upload["expires_at"] - time.time() + 2
                if remaining > ttl + 3:
                    raise AssertionError("gateway did not apply the selected upload TTL")
                if remaining > 0:
                    time.sleep(remaining)
                with httpx.Client(timeout=30) as blob:
                    sas = blob.put(upload["upload_url"], content=payload, headers=upload["headers"])
                ingest = client.post(prefix + "/documents/" + upload["doc_id"] + "/ingest", json={
                    "upload_id": upload["upload_id"], "idempotency_key": "expired-" + self.run_id})
                if (sas.status_code, ingest.status_code) != (403, 403):
                    raise AssertionError("expired SAS and registered ingestion were not both rejected")
                return {"config_revision": revision, "issued_upload_id": upload["upload_id"],
                        "expires_at": upload["expires_at"], "sas_status": sas.status_code,
                        "ingestion_status": ingest.status_code}
        finally:
            self.publish_change(lambda work: configure(work, restore=True),
                                "verify: restore gateway upload lifetime [skip ci]")
            await_value(lambda: settled(original["value"]), timeout=600, interval=10,
                        label="restored gateway upload lifetime")

    def persistence(self):
        before = self.d.kube("-n", "medw", "get", "pvc", "storage-qdrant-0", "-o", "json", json_result=True)
        old = self.pods("qdrant")
        self.d.kube("-n", "medw", "rollout", "restart", "statefulset/qdrant")
        self.d.kube("-n", "medw", "rollout", "status", "statefulset/qdrant", "--timeout=5m")
        after = self.d.kube("-n", "medw", "get", "pvc", "storage-qdrant-0", "-o", "json", json_result=True)
        if before["metadata"]["uid"] != after["metadata"]["uid"] or before["spec"]["volumeName"] != after["spec"]["volumeName"]:
            raise AssertionError("Qdrant persistent volume binding changed")
        current = self.pods("qdrant")
        if {p["metadata"]["uid"] for p in old} & {p["metadata"]["uid"] for p in current}:
            raise AssertionError("Qdrant pod did not restart")
        if not self.completed_workflows:
            raise NotVerified("indexing workflow must run before persistence verification")
        # Search retained indexed bytes before uploading any new source.
        with httpx.Client(verify=self.tls, timeout=30, headers={"Authorization": "Bearer " + self.token}) as client:
            response = client.post(self.base + f"/studies/{quote(self.d.config['study_id'])}/search",
                                   json={"query": "Operational acceptance", "top_k": 5})
            hits = response.raise_for_status().json()["hits"]
            expected_source = self.completed_workflows[-1]["source_revision"]
            if not any(hit.get("citation", {}).get("source_revision") == expected_source for hit in hits):
                raise AssertionError("uploaded source was not searchable after restart")
        return {"pvc_uid": after["metadata"]["uid"], "volume": after["spec"]["volumeName"],
                "pod_uid": current[0]["metadata"]["uid"], "retained_citations": [h["citation"] for h in hits]}

    def _job(self, name, template):
        metadata = {"name": name, "namespace": "medw", "labels": {"medw-verification": self.run_id}}
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata,
               "spec": {**template["spec"]["jobTemplate"]["spec"], "activeDeadlineSeconds": 300,
                        "backoffLimit": 0}}
        self.d.apply(job)
        self.d.kube("-n", "medw", "wait", "job/" + name, "--for=condition=Complete", "--timeout=5m")
        output = self.d.kube("-n", "medw", "logs", "job/" + name)
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
        template = self.d.kube("-n", "medw", "get", "cronjob", "qdrant-backup", "-o", "json", json_result=True)
        source = self.d.kube("-n", "medw", "get", "statefulset", "qdrant", "-o", "json", json_result=True)
        if source["spec"]["replicas"] != 1:
            raise NotVerified("this bounded acceptance restores the configured single-node topology")
        try:
            events = self._job(backup_name, template)
            uploaded = next((e for e in events if e.get("event") == "backup_uploaded"), None)
            if uploaded is None or not re.fullmatch(r"[A-Za-z0-9_/-]+", uploaded.get("key", "")):
                raise AssertionError("backup job did not report a committed Blob manifest")
            image = source["spec"]["template"]["spec"]["containers"][0]["image"]
            self.d.apply(
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
            self.d.kube("-n", "medw", "wait", "pod/" + name, "--for=condition=Ready", "--timeout=3m")
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
            self.d.kube("-n", "medw", "delete", "job", backup_name, restore_name, "--ignore-not-found", "--wait=false")
            self.d.kube("-n", "medw", "delete", "pod,service,networkpolicy", name, "--ignore-not-found", "--wait=false")
            self.d.kube("-n", "medw", "delete", "networkpolicy", name + "-target", "--ignore-not-found")

    def monitoring_and_scaling(self):
        if not self.token or not self.completed_workflows:
            raise NotVerified("a completed authenticated workflow is required before load verification")
        def replicas():
            value = self.d.kube("-n", "medw", "get", "deployment", "generation", "-o", "json", json_result=True)
            return value["status"].get("readyReplicas", 0)
        await_value(lambda: replicas() == 1, timeout=240, label="generation baseline of one replica")
        stop = threading.Event()
        counts = {"completed": 0, "failed": 0}
        lock = threading.Lock()

        def load():
            with httpx.Client(verify=self.tls, timeout=60, headers={"Authorization": "Bearer " + self.token}) as client:
                while not stop.is_set():
                    try:
                        response = client.post(self.base + f"/studies/{quote(self.d.config['study_id'])}/sections/"
                            + quote(self.d.config["section_path"]) + "/draft",
                            json={"query": "Operational acceptance", "top_k": 8, "max_tokens": 128})
                        response.raise_for_status()
                        if not any(json.loads(line).get("type") == "complete" for line in response.text.splitlines()):
                            raise ValueError("uncommitted draft")
                        with lock:
                            counts["completed"] += 1
                    except (httpx.HTTPError, ValueError, OSError):
                        with lock:
                            counts["failed"] += 1
        with self.forward("monitoring", "svc/prometheus-operated", 9090) as address, httpx.Client(timeout=20) as prometheus:
            def query(expression):
                result = prometheus.get(address + "/api/v1/query", params={"query": expression}).raise_for_status().json()
                if result.get("status") != "success":
                    raise AssertionError("Prometheus query failed")
                return result["data"]["result"]
            series = query(f"sum by(app) ({INFLIGHT_METRIC})")
            observed = {row["metric"].get("app") for row in series}
            if not set(SERVICES) <= observed:
                raise AssertionError("Prometheus is missing application metrics")
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(load) for _ in range(8)]
                try:
                    await_value(lambda: replicas() == 2, timeout=180, label="KEDA scale-up under real drafting")
                    gauge = query(f'sum({INFLIGHT_METRIC}{{app="generation"}})')
                finally:
                    stop.set()
                    for future in futures:
                        future.result(timeout=70)
            if not counts["completed"] or counts["failed"]:
                raise AssertionError("load included unsuccessful or uncommitted requests")
            await_value(lambda: replicas() == 1, timeout=240, label="KEDA scale-down after drafting")
            return {"metric_services": sorted(observed), "replicas": [1, 2, 1],
                    "real_drafts": counts, "inflight_sample": gauge}

    def traces(self):
        ids = sorted(set(correlation_ids(self.completed_workflows)))
        if not ids:
            raise NotVerified("workflow evidence contains no correlation IDs")
        resource = json.loads(command(["az", "resource", "show", "--ids", self.d.state["resources"]["insights"], "-o", "json"]))
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
        actual = json.loads(self.d.kube("-n", "medw", "exec", "deployment/ingestion-worker", "--", "python", "-c", script))
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
        output = self.d.kube("-n", "medw", "exec", "-i", "deployment/generation", "--", "python", "-c", script,
                             payload=json.dumps({"token": token, "events": events}))
        rows = json.loads(output)
        for row in rows:
            row["event_id"] = str(uuid.UUID(row["event_id"]))
        if len(rows) != len(events):
            raise AssertionError("a completed draft is missing its SQL audit row")
        return sorted(rows, key=lambda row: row["event_id"])

    @contextlib.contextmanager
    def checkout(self):
        branch = self.d.config["git_branch"]
        command(["git", "fetch", "origin", branch], cwd=self.d.root)
        with tempfile.TemporaryDirectory(prefix="medw-verification-") as temporary:
            work = pathlib.Path(temporary) / "checkout"
            command(["git", "worktree", "add", "--detach", str(work), "FETCH_HEAD"], cwd=self.d.root)
            try:
                yield work
            finally:
                command(["git", "worktree", "remove", "--force", str(work)], cwd=self.d.root)

    def publish_change(self, transform, message):
        with self.checkout() as work:
            targets = transform(work)
            command(["git", "add", *[str(path.relative_to(work)) for path in targets]], cwd=work)
            command(["git", "-c", "user.name=Medwriter Verification", "-c",
                     "user.email=verification@medwriter.invalid", "commit", "-m", message], cwd=work)
            revision = command(["git", "rev-parse", "HEAD"], cwd=work)
            # No force-push or reset: concurrent Git changes abort this operation.
            command(["git", "push", "origin", "HEAD:refs/heads/" + self.d.config["git_branch"]], cwd=work)
        return revision

    def select_release(self, bundle):
        path = self.d.directory / (bundle["bundle_sha"].split(":")[1] + ".json")
        path.write_text(json.dumps(bundle))
        import sys
        command([sys.executable, str(self.d.root / "scripts/commit_release.py"), str(path),
                 "--environment", "dev", "--branch", self.d.config["git_branch"],
                 "--allow-rollback", "--push"], cwd=self.d.root)

    def await_release(self, expected):
        def selected():
            release = self.d.kube("-n", "medw", "get", "helmrelease", "generation", "-o", "json", json_result=True)
            matches = release["spec"].get("values", {}).get("config", {}).get("release_bundle_sha") == expected
            return matches and any(c["type"] == "Ready" and c["status"] == "True" and
                c.get("observedGeneration") == release["metadata"]["generation"]
                for c in release.get("status", {}).get("conditions", []))
        await_value(selected, timeout=900, interval=15, label="Flux/Helm release selection " + expected[:18])
        return self.ready_releases()

    def failed_deployment(self):
        original = {}
        path = "deploy/flux/dev/environment-values.yaml"
        def break_repository(work):
            target = work / path
            documents = list(yaml.safe_load_all(target.read_text()))
            item = next(row for row in documents if row["metadata"]["name"] == "generation")
            original["repository"] = item["spec"]["values"]["image"]["repository"]
            original["timeout"] = item["spec"].get("timeout")
            item["spec"]["values"]["image"]["repository"] += "-verification-missing-" + self.run_id
            item["spec"]["timeout"] = "60s"
            target.write_text(yaml.safe_dump_all(documents, sort_keys=False))
            return [target]
        def restore_repository(work):
            target = work / path
            documents = list(yaml.safe_load_all(target.read_text()))
            item = next(row for row in documents if row["metadata"]["name"] == "generation")
            expected = original["repository"] + "-verification-missing-" + self.run_id
            if item["spec"]["values"]["image"]["repository"] != expected:
                raise RuntimeError("generation repository changed concurrently; refusing to overwrite")
            item["spec"]["values"]["image"]["repository"] = original["repository"]
            if original["timeout"] is None:
                item["spec"].pop("timeout", None)
            else:
                item["spec"]["timeout"] = original["timeout"]
            target.write_text(yaml.safe_dump_all(documents, sort_keys=False))
            return [target]
        revision = self.publish_change(break_repository, "verify: exercise failed deployment [skip ci]")
        try:
            def failure():
                release = self.d.kube("-n", "medw", "get", "helmrelease", "generation", "-o", "json", json_result=True)
                repository = release["spec"].get("values", {}).get("image", {}).get("repository", "")
                if not repository.endswith("-verification-missing-" + self.run_id):
                    return None
                conditions = release.get("status", {}).get("conditions", [])
                rolled_back = any(c["type"] == "Remediated" and c["status"] == "True" and
                                  c.get("observedGeneration") == release["metadata"]["generation"] and
                                  "Rollback" in c.get("reason", "") for c in conditions)
                return release.get("status") if rolled_back else None
            status = await_value(failure, timeout=600, interval=15, label="Helm failed-upgrade rollback remediation")
            return {"failed_config_revision": revision, "observed_helm_status": status}
        finally:
            self.publish_change(restore_repository, "verify: restore working generation repository [skip ci]")
            def restored():
                release = self.d.kube("-n", "medw", "get", "helmrelease", "generation", "-o", "json", json_result=True)
                repository = release["spec"].get("values", {}).get("image", {}).get("repository")
                return repository == original["repository"] and any(
                    condition["type"] == "Ready" and condition["status"] == "True" and
                    condition.get("observedGeneration") == release["metadata"]["generation"]
                    for condition in release.get("status", {}).get("conditions", []))
            await_value(restored, timeout=600, interval=15, label="restored working Helm configuration")

    def release_cycle(self):
        if not self.completed_workflows:
            raise NotVerified("release verification requires an initial audited workflow")
        initial = self.ready_releases()
        bundle_sha = initial["services"]["generation"]["bundle_sha"]
        with self.checkout() as work:
            bundle = json.loads((work / "deploy/releases" / (bundle_sha.split(":")[1] + ".json")).read_text())
        # Capture A after recovery checks finish changing the study. Both
        # comparisons use one fixed document, unchanged bytes and the same query.
        first = self.application()
        before = self.audit_rows([first["draft"]["draft_id"]])
        marker = "Operational acceptance marker " + self.run_id
        def change_prompt(work):
            target = work / "services/generation/app/prompts/section_draft.md"
            target.write_text(target.read_text() + "\n" + marker + "\n")
            return [target]
        changed = self.publish_change(change_prompt, "verify: change placeholder prompt [skip ci]")
        try:
            # Explicit queue prevents a second automatically triggered build for
            # this controlled acceptance edit. Normal source triggers remain on.
            pipeline = self.d.queue_release()
            def replacement():
                release = self.d.kube("-n", "medw", "get", "helmrelease", "generation", "-o", "json", json_result=True)
                values = release["spec"].get("values", {})
                return values.get("config", {}).get("release_bundle_sha") if values.get("image", {}).get("sourceSha") == changed else None
            selected = await_value(replacement, timeout=900, interval=15, label="release B selected by Flux")
            upgraded = self.await_release(selected)
            second = self.application()
            if first["draft"]["provenance"]["prompt_bundle_sha"] == second["draft"]["provenance"]["prompt_bundle_sha"]:
                raise AssertionError("packaged prompt did not change")
            first_citations = sorted(first["draft"]["citations"], key=lambda item: item["chunk_id"])
            second_citations = sorted(second["draft"]["citations"], key=lambda item: item["chunk_id"])
            if first_citations != second_citations:
                raise AssertionError("release comparison did not use identical retained source evidence")
            if first["output_sha256"] == second["output_sha256"]:
                raise AssertionError("placeholder output did not change with identical source evidence")
            after = self.audit_rows([first["draft"]["draft_id"], second["draft"]["draft_id"]])
            if next(row for row in after if row["event_id"] == before[0]["event_id"]) != before[0]:
                raise AssertionError("release A SQL provenance changed after release B")
            failure = self.failed_deployment()
            return {"initial": initial, "release_b": upgraded, "pipeline_run": pipeline,
                    "marker_source_sha": changed, "audits": after, "failed_deployment": failure}
        finally:
            self.select_release(bundle)
            self.report["rollback"] = self.await_release(bundle_sha)
            # Keep normal source at the pre-exercise prompt; the following source
            # release should not inadvertently reintroduce a verification marker.
            def restore_prompt(work):
                target = work / "services/generation/app/prompts/section_draft.md"
                content = target.read_text()
                target.write_text(content.replace("\n" + marker + "\n", ""))
                return [target]
            self.publish_change(restore_prompt, "verify: remove acceptance prompt marker [skip ci]")
            if self.audit_rows([first["draft"]["draft_id"]]) != before:
                raise AssertionError("release A audit changed during rollback")

    def run(self):
        self.check("release_readiness", self.ready_releases)
        self.check("public_access", self.public_access)
        self.check("access_and_checksum_rejections", self.negative_access)
        self.check("expired_upload_rejections", self.expired_upload)
        self.check("application_workflow", self.application)
        self.check("blob_source_checksum", self.blob_content)
        self.check("worker_restart_recovery", self.worker_recovery)
        self.check("qdrant_persistence", self.persistence)
        self.check("blob_snapshot_restore", self.backup_restore)
        self.check("metrics_and_scaling", self.monitoring_and_scaling)
        self.check("correlated_traces", self.traces)
        self.check("release_upgrade_rollback", self.release_cycle)
        self.report["passed"] = all(row["status"] == "passed" for row in self.report["checks"].values())
        self.save()
        return self.report


def verify(deployment, file=None, token=None, *, workflow=None):
    return Acceptance(deployment, file=file, token=token, workflow=workflow).run()

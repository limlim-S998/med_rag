#!/usr/bin/env python3
"""Run the normal application workflow in an isolated, disposable Compose project.

Uses the local storage adapters and a generated test JWT signing key. Service
calls use actual HTTP and packaged images; this does not claim an Azure or
NGINX deployment. Only this invocation's randomly named volumes are removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")
STUDY = "workflow-proof"


def run(*args: str, **kwargs) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("/tmp/medw-local-workflow-evidence.json"))
    args = parser.parse_args()
    project = "medw-application-proof-" + uuid.uuid4().hex[:8]
    environment = dict(os.environ)
    for service in (*SERVICES, "qdrant"):
        key = "INGESTION" if service == "ingestion-worker" else service.upper()
        environment[f"MEDW_{key}_PORT"] = "0"
    issuer = "https://workflow.invalid/issuer"
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update(kid="workflow", alg="RS256", use="sig")
    now = int(time.time())
    token = jwt.encode({"iss": issuer, "aud": "workflow-api", "tid": "workflow-tenant",
                        "oid": "workflow-writer", "iat": now, "nbf": now - 5, "exp": now + 900},
                       signing_key, algorithm="RS256", headers={"kid": "workflow"})
    headers = {"Authorization": "Bearer " + token}
    report: dict = {"result": "failed", "project": project, "image_tag": args.image_tag,
                    "backend": "local", "checks": [], "limits": [
                        "Uses SQLite/local artifacts instead of Azure services",
                        "Uses direct local service ports; edge NetworkPolicy/NGINX checked separately"]}
    with tempfile.TemporaryDirectory(prefix=project + "-") as temporary:
        directory = Path(temporary)
        (directory / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
        auth_environment = {"MEDW_SYNTHETIC_ENABLED": "false",
                            "MEDW_AUTH_TENANT_ID": "workflow-tenant",
                            "MEDW_AUTH_AUDIENCE": "workflow-api", "MEDW_AUTH_ISSUER": issuer,
                            "MEDW_AUTH_JWKS_URL": "http://identity:8080/jwks.json",
                            "MEDW_INGESTION_LEASE_SECONDS": "5"}
        overrides = {service: {"image": f"medw-{service}:{args.image_tag}",
                               "environment": auth_environment} for service in SERVICES}
        overrides["identity"] = {"image": f"medw-gateway:{args.image_tag}",
                                 "command": ["python", "-m", "http.server", "8080",
                                             "--directory", "/issuer"],
                                 "volumes": [f"{directory}:/issuer:ro"]}
        override = directory / "compose.json"
        override.write_text(json.dumps({"services": overrides}))
        command = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml"),
                   "-f", str(override), "--profile", "full"]

        def compose(*params: str) -> str:
            return run(*command, *params, env=environment)

        def container(service: str) -> str:
            return compose("ps", "-q", service)

        def inside(code: str) -> dict:
            return json.loads(run("docker", "exec", container("gateway"), "python", "-c", code))

        def snapshot() -> dict:
            return inside("""
import json,sqlite3
c=sqlite3.connect('/data/platform.sqlite3')
state=[{'kind':r[0],'study_id':r[1],'key':r[2],'value':json.loads(r[3])} for r in
       c.execute('SELECT kind,study_id,key,value FROM platform_state')]
audit=[{'kind':r[0],'value':json.loads(r[1])} for r in
       c.execute('SELECT kind,event_json FROM platform_audit')]
print(json.dumps({'state':state,'audit':audit}))
""")

        bases: dict[str, str] = {}
        client = httpx.Client(timeout=30)

        def refresh_ports() -> None:
            # Docker may allocate different ephemeral host ports on restart.
            for service in (*SERVICES, "qdrant"):
                details = json.loads(run("docker", "inspect", container(service)))[0]
                port = "6333/tcp" if service == "qdrant" else "8000/tcp"
                bases[service] = "http://127.0.0.1:" + details["NetworkSettings"]["Ports"][port][0]["HostPort"]

        def get_ready(service: str) -> int:
            try:
                return client.get(bases[service] + "/readyz").status_code
            except httpx.HTTPError:
                return 0

        def wait_ready() -> None:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                statuses = {name: get_ready(name) for name in SERVICES}
                if all(value == 200 for value in statuses.values()):
                    return
                time.sleep(1)
            raise AssertionError(f"services failed readiness: {statuses}")

        def request(method: str, url: str, expected: int = 200, **kwargs) -> httpx.Response:
            response = client.request(method, url, **kwargs)
            if response.status_code != expected:
                raise AssertionError(f"{method} {response.request.url.path}: "
                                     f"{response.status_code} {response.text[:300]}")
            return response

        def upload(doc_id: str, payload: bytes) -> dict:
            digest = hashlib.sha256(payload).hexdigest()
            registration = request("POST", bases["gateway"] + f"/studies/{STUDY}/documents:upload-url",
                                   201, headers=headers, json={"filename": doc_id + ".txt",
                                     "doc_id": doc_id, "size_bytes": len(payload), "sha256": digest}).json()
            request("PUT", registration["upload_url"], 201, content=payload)
            return registration

        def submit(registration: dict) -> dict:
            return request("POST", bases["ingestion-worker"]
                           + f"/studies/{STUDY}/documents/{registration['doc_id']}/ingest", 202,
                           headers=headers, json={"upload_id": registration["upload_id"],
                            "idempotency_key": registration["upload_id"]}).json()

        def job(identifier: str) -> dict:
            return request("GET", bases["ingestion-worker"]
                           + f"/studies/{STUDY}/jobs/{identifier}", headers=headers).json()

        def wait_job(identifier: str, *, checkpoint: str | None = None) -> dict:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                current = job(identifier)
                if current["state"] == "failed":
                    raise AssertionError(f"job failed: {current.get('error')}")
                if current["state"] == "done" or checkpoint in current["checkpoints"]:
                    return current
                time.sleep(0.2)
            raise AssertionError("durable job did not progress")

        try:
            compose("up", "-d", "--no-build")
            refresh_ports()
            wait_ready()
            report["versions"] = {service: request("GET", bases[service] + "/version").json()
                                   for service in SERVICES}
            report["checks"].append("all five packaged services are ready with the normal placeholder implementations")
            inside(f"""
import asyncio,json
from medw_core.persistence import SQLiteStateStore
from medw_core.local.platform import LocalStudyAccess
async def main():
 s=SQLiteStateStore('/data/platform.sqlite3')
 await LocalStudyAccess(s).grant('workflow-writer',{STUDY!r})
 await s.close()
 print(json.dumps({{'seeded':True}}))
asyncio.run(main())
""")
            endpoint = bases["gateway"] + f"/studies/{STUDY}/documents"
            request("GET", endpoint, 401)
            request("GET", endpoint.replace(STUDY, "other-study"), 403, headers=headers)
            first_bytes = b"First uploaded evidence: 12 blue flowers and immutable source bytes."
            first = upload("first-document", first_bytes)
            first_job = wait_job(submit(first)["id"])
            assert submit(first)["id"] == first_job["id"]
            before = snapshot()
            active_before = next(row["value"]["generation"] for row in before["state"]
                                 if row["kind"] == "active_index")
            second_bytes = b"Second uploaded evidence: 23 red birds; previous documents remain searchable."
            second = upload("second-document", second_bytes)
            compose("stop", "qdrant")
            second_job = submit(second)
            interrupted = wait_job(second_job["id"], checkpoint="embedding")
            checkpoints = interrupted["checkpoints"]
            assert interrupted["state"] != "done"
            partial = snapshot()
            assert next(row["value"]["generation"] for row in partial["state"]
                        if row["kind"] == "active_index") == active_before
            compose("stop", "ingestion-worker")
            compose("start", "qdrant", "ingestion-worker")
            refresh_ports()
            wait_ready()
            recovered = wait_job(second_job["id"])
            assert all(recovered["checkpoints"].get(name) == value for name, value in checkpoints.items())
            report["checks"].append("interrupted index publication preserves the active generation; worker restart resumes saved checkpoints")
            searched = request("POST", bases["retrieval"] + f"/studies/{STUDY}/search",
                               json={"query": "uploaded evidence", "top_k": 8}, headers=headers).json()
            assert len(searched["hits"]) == 2
            assert {hit["citation"]["source_revision"] for hit in searched["hits"]} == {
                first_job["source_revision"], recovered["source_revision"]}
            ranked = request("POST", bases["reranker"] + "/rerank", json={"query": "blue",
                "candidates": [{"id": hit["chunk_id"], "text": hit["text"]}
                               for hit in searched["hits"]], "top_k": 1}).json()
            assert "blue" in next(hit["text"] for hit in searched["hits"]
                                  if hit["chunk_id"] == ranked["results"][0]["id"])
            report["checks"].append("both documents survive study republishing and search/rerank HTTP calls return their evidence")
            url = bases["generation"] + f"/studies/{STUDY}/sections/1/draft"
            request("POST", url, 401, json={"query": "evidence"})
            request("POST", url, 422, headers=headers, json={"query": "evidence", "user_oid": "forged"})
            events = []
            started = time.monotonic()
            with client.stream("POST", url, json={"query": "uploaded evidence"}, headers=headers) as response:
                assert response.status_code == 200
                for data in response.iter_lines():
                    events.append(json.loads(data))
                    if len(events) == 1:
                        report["first_stream_event_ms"] = round((time.monotonic() - started) * 1000, 2)
            assert events[0]["type"] == "start" and events[-1]["type"] == "complete"
            output = "".join(event["text"] for event in events if event["type"] == "delta")
            assert "medical verification not performed" in output
            complete = events[-1]
            assert complete["verification"]["status"] == "not_performed"
            assert hashlib.sha256(output.encode()).hexdigest() == complete["output_sha256"]
            accept_url = bases["gateway"] + f"/studies/{STUDY}/sections/1/accept"
            accepted = request("POST", accept_url, headers=headers,
                               json={"draft_id": complete["draft_id"]}).json()
            assert accepted["status"] == "accepted"
            compose("restart", "gateway", "generation", "ingestion-worker")
            refresh_ports()
            accept_url = bases["gateway"] + f"/studies/{STUDY}/sections/1/accept"
            wait_ready()
            assert request("POST", accept_url, headers=headers,
                           json={"draft_id": complete["draft_id"]}).json()["status"] == "accepted"
            final = snapshot()
            audit = next(row["value"] for row in final["audit"] if row["kind"] == "generation"
                         and row["value"]["event_id"] == complete["event_id"])
            assert audit["output_text"] == output and audit["user_oid"] == "workflow-writer"
            manifest = searched["index_generation"]
            count = request("GET", bases["qdrant"] + f"/collections/{manifest['dense_collection']}").json()
            assert count["result"]["points_count"] == manifest["chunk_count"] == 2
            sources = [row["value"] for row in final["state"] if row["kind"] == "source"]
            for source in sources:
                checked = inside(f"""
import hashlib,json,pathlib
p=pathlib.Path('/data/artifacts') / {source['content_sha256']!r}
print(json.dumps({{'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}))
""")
                assert checked["sha256"] == source["content_sha256"]
            assert len([row for row in final["audit"] if row["kind"] == "index"]) == 2
            processing = []
            for completed in (first_job, recovered):
                checkpoints = completed["checkpoints"]
                artifacts = inside(f"""
import json,pathlib
root=pathlib.Path('/data/artifacts')
uris={checkpoints!r}
print(json.dumps({{stage:json.loads((root / uris[stage].removeprefix('sha256:')).read_text())
                  for stage in ('extracting','classifying')}}))
""")
                extracted, classified = artifacts["extracting"], artifacts["classifying"]
                assert extracted["implementation"] == "placeholder-text-1"
                assert classified["classifier_version"] == "placeholder-1"
                assert classified["source_sha256"] == extracted["source_sha256"]
                assert classified["table_type"] == "other" and classified["confidence"] == 0.0
                assert classified["input_kind"] == "placeholder_envelope"
                assert extracted["medical_parsing"] is classified["medical_parsing"] is False
                processing.append({"job_id": completed["id"],
                                   "source_sha256": extracted["source_sha256"],
                                   "parser_version": extracted["implementation"],
                                   "classifier_version": classified["classifier_version"],
                                   "table_type": classified["table_type"],
                                   "input_kind": classified["input_kind"], "medical_parsing": False})
            report.update(result="passed", draft_id=complete["draft_id"], event_id=complete["event_id"],
                          output_sha256=complete["output_sha256"], index_generation=manifest,
                          source_checksums=[source["content_sha256"] for source in sources],
                          job_ids=[first_job["id"], recovered["id"]],
                          correlation_id=audit["correlation_id"], stream_events=len(events),
                          processing_provenance=processing)
            report["checks"].extend([
                "stored source byte checksums match registered uploads",
                "streamed output contains retrieved evidence and commits complete provenance before success",
                "authenticated specific-draft acceptance and persisted job/audit state survive container restarts",
                "missing authentication, cross-study access and forged actor fields fail",
                "repeated ingestion does not append duplicate index audit"])
            report["checks"].append(
                "extraction and classifier interfaces persist actual placeholder identities and source checksums")
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            client.close()
            try:
                compose("down", "-v", "--remove-orphans")
                report["cleanup"] = "removed disposable project and volumes"
            finally:
                args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"result": report["result"], "checks": report["checks"],
                          "evidence": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()

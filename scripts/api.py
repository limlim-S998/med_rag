#!/usr/bin/env python3
"""Exercise the normal application APIs and save evidence without credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import ssl
import time
import uuid
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]


def workflow(base_url: str, file: str | pathlib.Path, study: str, section: str,
             token: str, ca_file: str | pathlib.Path | None = None, *, timeout: float = 300,
             doc_id: str | None = None, on_submitted: Callable[[dict], None] | None = None,
             processing: str = "immediate", submit_only: bool = False,
             on_progress: Callable[[str, dict], None] | None = None) -> dict:
    """A client, not another implementation of the application's workflow."""
    payload = pathlib.Path(file).read_bytes()
    if not 0 < len(payload) <= 5 * 1024 * 1024:
        raise ValueError("file must contain between 1 byte and 5 MiB")
    if urlsplit(base_url).scheme != "https" and urlsplit(base_url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("remote application connections require HTTPS")
    trust = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    cid, checksum = uuid.uuid4().hex, hashlib.sha256(payload).hexdigest()
    prefix = "/studies/" + quote(study, safe="")
    evidence: dict = {"schema_version": 1, "correlation_id": cid, "study_id": study,
                "input": {"sha256": checksum, "size_bytes": len(payload)}, "checks": {}}
    def progress(stage, **detail):
        if on_progress:
            on_progress(stage, detail)

    with httpx.Client(base_url=base_url.rstrip("/"), verify=trust, timeout=timeout,
                      headers={"Authorization": "Bearer " + token, "x-correlation-id": cid}) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            # Do not include response bodies or URLs containing upload tokens in exceptions.
            if not response.is_success:
                raise RuntimeError(f"application {method} failed with HTTP {response.status_code}")
            return response.json()

        version = request("GET", "/version")
        registration = request("POST", prefix + "/documents:upload-url", json={
            "filename": pathlib.Path(file).name, "size_bytes": len(payload), "sha256": checksum,
            **({"doc_id": doc_id} if doc_id else {})})
        # The upload URL is a capability. Never attach the writer's JWT or save it.
        upload_url = registration["upload_url"]
        upload_trust = trust if urlsplit(upload_url).netloc == urlsplit(base_url).netloc else True
        with httpx.Client(verify=upload_trust, timeout=timeout) as uploader:
            uploaded = uploader.put(upload_url, content=payload, headers=registration.get("headers", {}))
            if not uploaded.is_success:
                raise RuntimeError(f"blob upload failed with HTTP {uploaded.status_code}")
        progress("uploaded", doc_id=registration["doc_id"], sha256=checksum, size_bytes=len(payload))
        body = {"upload_id": registration["upload_id"], "idempotency_key": uuid.uuid4().hex,
                "processing": processing}
        ingest_path = prefix + "/documents/" + quote(registration["doc_id"], safe="") + "/ingest"
        job = request("POST", ingest_path, json=body)
        repeated = request("POST", ingest_path, json=body)
        if job["id"] != repeated["id"]:
            raise AssertionError("idempotent submission created a second job")
        evidence["checks"]["idempotent_submission"] = True
        evidence.update(upload_id=registration["upload_id"], doc_id=registration["doc_id"],
                        job_id=job["id"], source_revision=job["source_revision"],
                        processing=processing, state=job["state"], release=version)
        if on_submitted:
            on_submitted(job)
        progress("submitted", job_id=job["id"], state=job["state"], source_revision=job["source_revision"])
        if submit_only:
            evidence["checks"]["source_preserved"] = bool(job["source_revision"])
            evidence["checks"]["scheduled_for_batch"] = processing == "nightly" and job["state"] == "scheduled"
            return evidence
        deadline = time.monotonic() + timeout
        while job["state"] not in {"done", "failed", "superseded"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("ingestion deadline exceeded")
            time.sleep(0.5)
            previous = job["state"]
            job = request("GET", prefix + "/jobs/" + quote(job["id"], safe=""))
            if job["state"] != previous:
                progress("job", job_id=job["id"], state=job["state"])
        if job["state"] != "done":
            raise RuntimeError("ingestion job failed; inspect its correlation ID")
        documents = request("GET", prefix + "/documents")
        document = next(row for row in documents if row["doc_id"] == registration["doc_id"])
        query = "sha256:" + checksum
        search = request("POST", prefix + "/search", json={"query": query, "top_k": 8})
        if not any(hit["citation"]["source_revision"] == job["source_revision"] for hit in search["hits"]):
            raise AssertionError("retrieval did not return the uploaded source")
        progress("retrieved", hits=len(search["hits"]), source_revision=job["source_revision"])
        lines, text_parts, elapsed = [], [], []
        began = time.monotonic()
        with client.stream("POST", prefix + "/sections/" + quote(section, safe="") + "/draft",
                           json={"query": query, "top_k": 8}) as response:
            if not response.is_success:
                raise RuntimeError(f"draft failed with HTTP {response.status_code}")
            for raw in response.iter_lines():
                if not raw:
                    continue
                event = json.loads(raw)
                lines.append(event)
                elapsed.append(time.monotonic() - began)
                if event["type"] == "delta":
                    text_parts.append(event["text"])
                    progress("draft_delta", text=event["text"])
        output = "".join(text_parts)
        if not lines or lines[-1]["type"] != "complete":
            raise AssertionError("stream ended without durable audit/draft completion")
        complete = lines[-1]
        if checksum not in output or complete["output_sha256"] != hashlib.sha256(output.encode()).hexdigest():
            raise AssertionError("draft output does not preserve source/checksum identity")
        accepted = request("POST", prefix + "/sections/" + quote(section, safe="") + "/accept",
                           json={"draft_id": complete["draft_id"]})
        if accepted["draft_id"] != complete["draft_id"] or accepted["status"] != "accepted":
            raise AssertionError("acceptance changed the wrong draft")
        progress("accepted", draft_id=complete["draft_id"], output_sha256=complete["output_sha256"])
        evidence.update(upload_id=registration["upload_id"], document=document,
                        job_id=job["id"], source_revision=job["source_revision"],
                        job_checkpoints=job["checkpoints"], state=job["state"],
                        batch_id=job.get("batch_id"), release=version,
                        index_generation=search["index_generation"],
                        draft=complete, acceptance=accepted,
                        stream={"events": len(lines), "delta_events": len(text_parts),
                                "first_event_seconds": elapsed[0], "duration_seconds": elapsed[-1]},
                        output_sha256=complete["output_sha256"])
        evidence["checks"].update(ingestion_done=True, retrieved_uploaded_source=True,
                                  streamed_draft=len(text_parts) > 1, accepted_specific_draft=True,
                                  medical_verification_not_performed=(
                                      complete["verification"]["status"] == "not_performed"))
    return evidence


def acquire_token(config: dict, cache_file: pathlib.Path | None = None, *,
                  minimum_validity_seconds: int = 900) -> str:
    """Return an API token with enough lifetime for the next bounded operation.

    MSAL reports remaining lifetime, including for cache hits. Refresh an
    almost-expired token before starting work; never expose credentials in
    diagnostics or treat a locally decoded JWT as authentication evidence.
    """
    import msal
    if minimum_validity_seconds < 0:
        raise ValueError("minimum token validity must not be negative")

    def valid_for_operation(result):
        if not result or not result.get("access_token"):
            return False
        try:
            return int(result["expires_in"]) >= minimum_validity_seconds
        except (KeyError, TypeError, ValueError):
            return False

    cache = msal.SerializableTokenCache()
    if cache_file and cache_file.exists():
        os.chmod(cache_file, 0o600)
        cache.deserialize(cache_file.read_text())
    client = msal.PublicClientApplication(config["user_client_id"],
                                         authority="https://login.microsoftonline.com/" + config["tenant_id"],
                                         token_cache=cache)
    scopes = ["api://" + config["api_client_id"] + "/access"]
    try:
        expected_user = config.get("writer_object_id")
        for account in client.get_accounts():
            if expected_user and account.get("local_account_id") != expected_user:
                continue
            result = client.acquire_token_silent(scopes, account=account)
            if result and "access_token" in result and not valid_for_operation(result):
                result = client.acquire_token_silent(scopes, account=account, force_refresh=True)
            if valid_for_operation(result):
                return result["access_token"]
        method = config.get("api_login_method", "browser")
        if method == "browser":
            port = int(config.get("api_login_port", 8400))
            if not 1024 <= port <= 65535:
                raise ValueError("api_login_port must be between 1024 and 65535")
            message = f"Open http://localhost:{port} in a browser on this computer to sign in."
            print(message, flush=True)
            result = client.acquire_token_interactive(
                scopes=scopes, port=port, timeout=600,
                welcome_template="<html><body><a href='$auth_uri'>Sign in to the medical writer API</a></body></html>",
                auth_uri_callback=lambda _: print(message, flush=True))
        elif method == "device":
            flow = client.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise RuntimeError("could not start API sign-in")
            print(flow["message"], flush=True)
            result = client.acquire_token_by_device_flow(flow)
        else:
            raise ValueError("api_login_method must be browser or device")
        if "access_token" not in result:
            raise RuntimeError("API sign-in failed: " + result.get("error", "unknown"))
        if not valid_for_operation(result):
            raise RuntimeError("API token lifetime is insufficient for the requested operation")
        return result["access_token"]
    finally:
        if cache_file and cache.has_state_changed:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(cache_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(cache.serialize())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=pathlib.Path, required=True)
    parser.add_argument("--study", required=True)
    parser.add_argument("--section", required=True)
    parser.add_argument("--user-oid", help="Optional expected writer object ID for the MSAL cache")
    parser.add_argument("--file", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "data/evidence/application.json")
    parser.add_argument("--cache", type=pathlib.Path, default=ROOT / "data/api/token-cache.json")
    parser.add_argument("--processing", choices=("immediate", "nightly"), default="immediate")
    args = parser.parse_args()
    resources = json.loads(args.resources.read_text())
    resources["writer_object_id"] = args.user_oid
    token = os.environ.get("MEDW_API_TOKEN") or acquire_token(resources, args.cache)
    report = workflow("https://" + resources["hostname"], args.file, args.study,
                      args.section, token, processing=args.processing,
                      submit_only=args.processing == "nightly")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, indent=2)
    print(f"Application evidence: {args.output}")


if __name__ == "__main__":
    main()

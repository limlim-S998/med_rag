"""Batch admission, publication ordering, restart recovery and authenticated APIs."""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from support.stores import SQLiteStudyAccess
from test_ingestion_workflow import workflow

from medw_core.auth import TokenValidator
from medw_core.batches import BatchStore
from medw_core.persistence import Conflict
from medw_core.settings import Settings
from medw_core.uploads import UploadRequest
from services.ingestion_worker.app import main as worker


@pytest.fixture
async def batch_flow(tmp_path, monkeypatch):
    values, runner = workflow(tmp_path)
    values["jobs"].clock = lambda: 1000.0
    batches = BatchStore(values["state"], values["jobs"], clock=lambda: 2000.0)
    try:
        yield values, runner, batches
    finally:
        await values["audit"].close()
        await values["state"].close()


async def submit(values, *, key="one", doc="doc", study="S1", processing="nightly", content=b"12 patients"):
    source = await values["evidence"].ingest_source(study, doc, content, doc + ".txt")
    return await values["jobs"].create(study, doc, source_revision=source.revision_id,
                                      idempotency_key=key, processing=processing,
                                      requested_by_oid="writer", correlation_id="request-" + key)


async def test_nightly_jobs_wait_for_airflow_and_do_not_block_immediate_work(batch_flow):
    values, runner, batches = batch_flow
    night = await submit(values)
    assert night["state"] == "scheduled"
    assert await runner.run_once() == 0
    assert not await values["jobs"].recoverable()
    with pytest.raises(Conflict, match="Airflow"):
        await values["jobs"].claim("S1", night["id"], "bypass")
    immediate = await submit(values, key="now", doc="other", processing="immediate")
    assert await runner.run_once() == 1
    assert (await values["jobs"].get("S1", immediate["id"]))["state"] == "done"
    batch = await batches.prepare("night-1", 1500, "airflow", "batch-correlation")
    assert batch["counts"]["running"] == 1
    await runner.run_once()
    result = await batches.status(batch["id"])
    assert result["status"] == "completed" and result["counts"]["done"] == 1
    event = json.loads(values["audit"].connection.execute(
        "SELECT event_json FROM platform_audit WHERE kind='index' ORDER BY rowid DESC").fetchone()[0])
    assert event["batch_id"] == batch["id"] and event["job_id"] == night["id"]
    assert event["requested_by_oid"] == "writer" and event["correlation_id"] == "request-one"
    generation, _ = await values["index_registry"].active("S1")
    assert generation.chunk_count == 2


async def test_retry_keeps_frozen_selection_and_recovers_partial_dispatch(batch_flow, monkeypatch):
    values, runner, batches = batch_flow
    first = await submit(values)
    second = await submit(values, key="two", doc="two")
    admit = values["jobs"].admit_batch
    calls = 0

    async def interrupted(*args):
        nonlocal calls
        calls += 1
        result = await admit(*args)
        if calls == 1:
            raise ConnectionError("lost admission acknowledgment")
        return result

    monkeypatch.setattr(values["jobs"], "admit_batch", interrupted)
    with pytest.raises(ConnectionError):
        await batches.prepare("night-1", 1500, "airflow", "correlation")
    later = await submit(values, key="late", doc="late")
    monkeypatch.setattr(values["jobs"], "admit_batch", admit)
    restarted = BatchStore(values["state"], values["jobs"], clock=lambda: 2000)
    batch = await restarted.prepare("night-1", 1500, "airflow", "correlation")
    assert {i["job_id"] for i in batch["items"]} == {first["id"], second["id"]}
    assert (await values["jobs"].get("S1", later["id"]))["state"] == "scheduled"
    await runner.run_once()
    await runner.run_once()
    assert (await restarted.prepare("night-1", 1500, "airflow", "correlation"))["counts"]["done"] == 2
    with pytest.raises(Conflict, match="cutoff"):
        await restarted.prepare("night-1", 1600, "airflow", "correlation")


async def test_cutoff_and_limit_leave_later_documents_pending(batch_flow):
    values, _, batches = batch_flow
    old = await submit(values)
    await submit(values, key="two", doc="two")
    values["jobs"].clock = lambda: 1500
    late = await submit(values, key="late", doc="late")
    batches.limit = 1
    result = await batches.prepare("night", 1500, "airflow", "correlation")
    assert len(result["items"]) == 1 and result["deferred_by_limit"] == 1
    assert (await values["jobs"].get("S1", late["id"]))["state"] == "scheduled"
    assert old["state"] == "scheduled"  # callers' snapshots are not mutated


async def test_old_nightly_revision_cannot_overwrite_new_immediate_revision(batch_flow):
    values, runner, batches = batch_flow
    old = await submit(values, content=b"Old 12 patients")
    values["jobs"].clock = lambda: 1100
    newer = await submit(values, key="new", processing="immediate", content=b"New 24 patients")
    await runner.run_once()
    active, _ = await values["index_registry"].active("S1")
    batch = await batches.prepare("night", 1500, "airflow", "correlation")
    await runner.run_once()
    assert (await values["jobs"].get("S1", old["id"]))["state"] == "superseded"
    assert (await values["jobs"].get("S1", newer["id"]))["state"] == "done"
    assert (await values["index_registry"].active("S1"))[0] == active
    assert (await batches.status(batch["id"]))["counts"]["superseded"] == 1


async def test_immediate_priority_does_not_overtake_a_recovering_publication(batch_flow, monkeypatch):
    values, runner, batches = batch_flow
    old = await submit(values)
    await batches.prepare("night", 1500, "airflow", "correlation")
    activate = values["index_registry"].activate

    async def interrupted(*args, **kwargs):
        await activate(*args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(values["index_registry"], "activate", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once()
    later = await submit(values, key="later", doc="later", processing="immediate")
    monkeypatch.setattr(values["index_registry"], "activate", activate)
    current = await values["jobs"].get("S1", old["id"])
    await values["jobs"]._save(current, next_attempt_at=0)
    await runner.run_once()
    assert (await values["jobs"].get("S1", old["id"]))["state"] == "done"
    assert (await values["jobs"].get("S1", later["id"]))["state"] == "queued"


async def test_failed_document_fails_batch_and_other_studies_are_not_disclosed(batch_flow):
    values, _, batches = batch_flow
    first = await submit(values)
    await submit(values, key="other", study="S2")
    batch = await batches.prepare("night", 1500, "airflow", "correlation")
    job = await values["jobs"].claim("S1", first["id"], "worker")
    await values["jobs"].fail(job, "queued", "dependency unavailable")
    view = await batches.status(batch["id"], study_id="S1")
    assert view["status"] == "failed" and len(view["items"]) == 1
    assert "S2" not in json.dumps(view) and "deferred_by_limit" not in view
    assert (await batches.status(batch["id"]))["status"] == "running"
    with pytest.raises(KeyError):
        await batches.status(batch["id"], study_id="S3")


async def test_concurrent_runs_admit_each_document_only_once(batch_flow):
    values, runner, batches = batch_flow
    job = await submit(values)
    results = await asyncio.gather(*(batches.prepare(name, 1500, "airflow", name) for name in ("a", "b")))
    await runner.run_once()
    assert await runner.run_once() == 0
    current = await values["jobs"].get("S1", job["id"])
    assert current["batch_id"] in {b["id"] for b in results}
    assert values["audit"].connection.execute("SELECT count(*) FROM platform_audit").fetchone()[0] == 1


async def test_real_signed_api_upload_choice_expiry_and_workload_boundary(batch_flow, monkeypatch):
    values, runner, _ = batch_flow
    values["jobs"].clock = time.time
    access = SQLiteStudyAccess(values["state"])
    await access.grant("writer", "S1")
    values["authorization"] = access
    config = Settings(_env_file=None, auth_tenant_id="tenant", auth_audience="api",
                      auth_issuer="https://identity/tenant", auth_jwks_url="https://identity/keys",
                      batch_principal_id="airflow")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "key"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"keys": [public]}))) as identity:
        monkeypatch.setattr(worker.app.state, "services", SimpleNamespace(
            require=values.__getitem__, authorization=access), raising=False)
        monkeypatch.setattr(worker.app.state, "settings", config, raising=False)
        monkeypatch.setattr(worker.app.state, "token_validator", TokenValidator(config, identity), raising=False)

        def headers(actor="writer", **claims):
            now = int(time.time())
            token = jwt.encode({"iss": config.auth_issuer, "aud": "api", "tid": "tenant", "oid": actor,
                                "iat": now, "nbf": now - 1, "exp": now + 600,
                                "azp": "airflow-client" if actor == "airflow" else "writer-client", **claims},
                               key, algorithm="RS256", headers={"kid": "key"})
            return {"Authorization": "Bearer " + token}

        body = UploadRequest(filename="binary.bin", size_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest(), doc_id="doc")
        registered = await values["uploads"].register("S1", body)
        record = await values["uploads"].record("S1", registered["upload_id"])
        await values["uploads"].storage.write(record, b"abc")
        request = {"upload_id": registered["upload_id"], "idempotency_key": "one", "processing": "nightly"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://worker") as client:
            url = "/studies/S1/documents/doc/ingest"
            assert (await client.post(url, json=request)).status_code == 401
            assert (await client.post(url.replace("S1", "S2"), json=request, headers=headers())).status_code == 403
            assert (await client.post(url, json={**request, "requested_by_oid": "fake"}, headers=headers())).status_code == 422
            response = await client.post(url, json=request, headers=headers())
            assert response.status_code == 202 and response.json()["state"] == "scheduled"
            job = response.json()
            values["uploads"].clock = lambda: time.time() + 10000
            repeated = await client.post(url, json=request, headers=headers())
            assert repeated.json()["id"] == job["id"]
            assert (await client.post(url, json={**request, "processing": "immediate"}, headers=headers())).status_code == 409
            assert (await client.post(url, json={**request, "idempotency_key": "new"}, headers=headers())).status_code == 403
            assert await runner.run_once() == 0
            batch_request = {"run_id": "night", "cutoff": time.time()}
            for forbidden in [headers(), headers("other-machine"), headers("airflow", scp="access")]:
                assert (await client.post("/_internal/batches", json=batch_request, headers=forbidden)).status_code == 403
            batch_response = await client.post("/_internal/batches", json=batch_request, headers=headers("airflow"))
            assert batch_response.status_code == 200
            await runner.run_once()
            url = "/studies/S1/batches/" + batch_response.json()["id"]
            assert (await client.get(url, headers=headers())) .json()["status"] == "completed"
            assert (await client.get(url, headers=headers("airflow"))).status_code == 403
            assert (await client.get(url.replace("S1", "S2"), headers=headers())).status_code == 403

"""Guard operational evidence and isolation without pretending to contact Azure."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.azure_verify import (
    CORRELATION_DIMENSION,
    SERVICES,
    Acceptance,
    NotVerified,
    complete_trace_rows,
    correlation_ids,
)


def collector(tmp_path):
    value = Acceptance.__new__(Acceptance)
    value.d = SimpleNamespace(directory=tmp_path, state={}, config={}, root=tmp_path)
    value.report = {"checks": {}, "passed": False}
    value.run_id = "1234567890"
    value.completed_workflows = []
    value.token_provider = None
    return value


def test_authentication_refreshes_between_checks_and_after_release_build(tmp_path):
    value = collector(tmp_path)
    credentials = iter(["first-private-token", "refreshed-private-token"])
    value.token_provider = lambda: next(credentials)
    value.check("before_build", lambda: {"observed": True})
    assert value.token == "first-private-token"
    value.base, value.ca, value.file = "https://api.invalid", tmp_path / "ca", tmp_path / "input"
    value.d.config = {"study_id": "study", "section_path": "section"}

    def workflow(**kwargs):
        assert kwargs["token"] == "refreshed-private-token"
        return {"checks": {"completed": True}}

    value.workflow = workflow
    value.application()
    assert "private-token" not in (tmp_path / "acceptance.json").read_text()


@pytest.mark.parametrize("rollback_failure", ["selection", "readiness"])
def test_release_prompt_cleanup_survives_failed_rollback(tmp_path, monkeypatch, rollback_failure):
    import contextlib

    value = collector(tmp_path)
    bundle = {"bundle_sha": "sha256:original"}
    selected = tmp_path / "deploy/releases/original.json"
    selected.parent.mkdir(parents=True)
    selected.write_text(json.dumps(bundle))
    prompt = tmp_path / "services/generation/app/prompts/section_draft.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Original prompt\n")
    value.completed_workflows = [{"draft": {"draft_id": "first"}}]
    monkeypatch.setattr(value, "ready_releases", lambda: {
        "services": {"generation": {"bundle_sha": bundle["bundle_sha"]}}})
    monkeypatch.setattr(value, "checkout", lambda: contextlib.nullcontext(tmp_path))
    monkeypatch.setattr(value, "application", lambda: value.completed_workflows[0])
    audit_reads = []

    def audit(events):
        audit_reads.append(events)
        return [{"event_id": "first"}]

    monkeypatch.setattr(value, "audit_rows", audit)
    monkeypatch.setattr(value, "publish_change", lambda transform, message: transform(tmp_path) and "source")

    def fail(*args):
        raise RuntimeError("simulated operational failure")

    value.d.queue_release = fail
    monkeypatch.setattr(value, "select_release", fail if rollback_failure == "selection" else lambda _: None)
    monkeypatch.setattr(value, "await_release", fail)
    with pytest.raises(RuntimeError):
        value.release_cycle()
    assert prompt.read_text() == "Original prompt\n"
    assert len(audit_reads) == 2


def test_all_release_revisions_must_settle_before_reading_pod_identities(tmp_path, monkeypatch):
    value = collector(tmp_path)
    snapshots = 0

    def kube(*args, **kwargs):
        nonlocal snapshots
        if "helmreleases" in args:
            snapshots += 1
            return {"items": [{"metadata": {"name": name, "generation": 2},
                "spec": {"values": {"image": {"digest": "sha256:image", "sourceSha": "source"},
                                    "config": {"release_bundle_sha": "selected"}}},
                "status": {"conditions": [{"type": "Ready", "status": "True", "observedGeneration":
                    1 if snapshots == 1 and name == "retrieval" else 2}]}}
                for name in SERVICES]}
        if "exec" in args:
            assert snapshots == 2
            return json.dumps({"image_digest": "sha256:image", "image_sha": "source"})
        return {"items": []}

    value.d.kube = kube
    monkeypatch.setattr("scripts.azure_verify.time.sleep", lambda _: None)
    monkeypatch.setattr(value, "pods", lambda name: [{"metadata": {"name": name, "uid": name},
        "status": {"conditions": [{"type": "Ready", "status": "True"}],
                   "containerStatuses": [{"imageID": "registry@sha256:image"}]}}])
    result = value.ready_releases(expected_bundle="selected")
    assert set(result["services"]) == set(SERVICES)


def test_failed_and_missing_observations_never_pass_or_leak_credentials(tmp_path):
    value = collector(tmp_path)

    def secret_failure():
        raise RuntimeError("https://blob/path?sig=SECRET")

    def unobserved():
        raise NotVerified("scale-up was not observed")

    assert value.check("bad", secret_failure) is None
    assert value.check("missing", unobserved) is None
    assert value.report["checks"]["bad"]["status"] == "failed"
    assert value.report["checks"]["missing"]["status"] == "not_verified"
    encoded = (tmp_path / "acceptance.json").read_text()
    assert "SECRET" not in encoded
    assert json.loads(encoded)["passed"] is False


def test_acceptance_requires_every_check_to_pass(tmp_path, monkeypatch):
    value = collector(tmp_path)
    names = ("ready_releases", "public_access", "negative_access", "expired_upload", "application", "blob_content", "worker_recovery",
             "persistence", "backup_restore", "monitoring_and_scaling", "traces", "release_cycle")
    for name in names:
        monkeypatch.setattr(value, name, lambda: {"observed": True})

    def missing():
        raise NotVerified("No pipeline access")

    monkeypatch.setattr(value, "release_cycle", missing)
    report = value.run()
    assert len(report["checks"]) == len(names)
    assert report["passed"] is False
    assert report["checks"]["release_upgrade_rollback"]["status"] == "not_verified"


def test_correlation_query_ignores_unsafe_values():
    value = {"correlation_id": "safe-123", "draft": {"trace_id": "abc:1"},
             "bad": {"correlation_id": "'); drop table anything"}, "token": "secret"}
    assert set(correlation_ids(value)) == {"safe-123", "abc:1"}


def test_collector_scaling_metric_matches_actual_prometheus_export_and_chart():
    import sys

    subprocess.run([sys.executable, "-c", """
import pathlib, yaml
from medw_core import metrics
from prometheus_client.parser import text_string_to_metric_families
from scripts.azure_verify import INFLIGHT_METRIC
metrics.configure_prometheus()
metrics.inflight_requests.add(2, {'app':'generation'})
body,_=metrics.render_prometheus()
samples=[sample for family in text_string_to_metric_families(body.decode()) for sample in family.samples]
assert any(sample.name==INFLIGHT_METRIC and sample.labels.get('app')=='generation' and sample.value==2 for sample in samples)
chart=yaml.safe_load(pathlib.Path('deploy/charts/generation/values.yaml').read_text())
assert 'medw_'+chart['autoscaling']['metric']==INFLIGHT_METRIC
"""], check=True)


@pytest.mark.asyncio
async def test_collector_insights_dimensions_match_real_middleware_and_azure_export_conversion():
    import httpx
    from azure.monitor.opentelemetry.exporter.export.trace._exporter import (
        _convert_span_to_envelope,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from medw_core.tracing import HEADER, TraceMiddleware

    roles = []
    for service in SERVICES:
        memory = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(SimpleSpanProcessor(memory))

        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = TraceMiddleware(application, service)
        middleware.tracer = provider.get_tracer("contract")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
            await client.get("https://api.invalid/work", headers={HEADER: "shared-workflow"})
        span = memory.get_finished_spans()[0]
        envelope = _convert_span_to_envelope(span)
        assert envelope.data.base_data.properties[CORRELATION_DIMENSION] == "shared-workflow"
        assert envelope.tags["ai.cloud.role"] == service
        roles.append(envelope.tags["ai.cloud.role"])
        provider.shutdown()
    table = {"columns": [{"name": name} for name in ("records", "roles", "cid")],
             "rows": [[5, json.dumps(roles), "shared-workflow"]]}
    assert complete_trace_rows(table)[0]["correlation_id"] == "shared-workflow"
    # Independent traces that collectively visit five services are insufficient.
    table["rows"] = [[3, roles[:3], "one-workflow"], [2, roles[3:], "another-workflow"]]
    assert complete_trace_rows(table) == []


@pytest.mark.parametrize("fail_restore", [False, True])
def test_snapshot_restore_is_isolated_and_cleanup_runs_on_failure(tmp_path, monkeypatch, capsys, fail_restore):
    import hashlib
    import importlib.util
    import pathlib
    import sys

    # Use actual producer CLI event names and the real upload prefix format;
    # replacing these with hand-authored events hid a double-slash report bug.
    source = pathlib.Path(__file__).resolve().parents[1] / "deploy/charts/qdrant/files/qdrant_backup.py"
    spec = importlib.util.spec_from_file_location("backup_contract", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = b"snapshot"
    manifest = {"format": 1, "id": "2026-id", "peer_count": 1, "collections": [{"snapshots": [{
        "peer": 0, "file": "0.snapshot", "sha256": hashlib.sha256(snapshot).hexdigest()}]}]}

    def backup(client, nodes, directory):
        directory = pathlib.Path(directory)
        (directory / "0.snapshot").write_bytes(snapshot)
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    uploaded_blobs = []
    monkeypatch.setattr(module, "backup", backup)
    monkeypatch.setattr(module, "restore", lambda *args: manifest)
    monkeypatch.setattr(module, "blob_container", lambda: SimpleNamespace(
        upload_blob=lambda name, *args, **kwargs: uploaded_blobs.append(name), list_blobs=lambda **kwargs: []))
    monkeypatch.setattr(module.signal, "signal", lambda *args: None)
    cli_events = {}
    for operation in ("backup", "restore"):
        monkeypatch.setattr(sys, "argv", ["backup", operation, *(["--upload"] if operation == "backup" else [])])
        module.main()
        cli_events[operation] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    value = collector(tmp_path)
    applied, calls, jobs = [], [], []
    value.completed_workflows = [{"source_revision": "retained-source"}]
    template = {"spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{
        "command": ["python", "backup", "backup", "--upload"],
        "env": [{"name": "QDRANT_NODES", "value": "http://qdrant:6333"}]}]}}}}}}

    def kube(*args, **kwargs):
        calls.append(args)
        if "cronjob" in args:
            return template
        if "statefulset" in args:
            return {"spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "qdrant@sha256:pin"}]}}}}
        return ""

    def job(name, specification):
        jobs.append((name, json.loads(json.dumps(specification))))
        if name.endswith("-backup"):
            return cli_events["backup"]
        if fail_restore:
            raise RuntimeError("restore failed")
        return cli_events["restore"]

    value.d.kube = kube
    value.d.apply = lambda *objects: applied.extend(objects)
    monkeypatch.setattr(value, "_job", job)
    if fail_restore:
        with pytest.raises(RuntimeError, match="restore failed"):
            value.backup_restore()
    else:
        result = value.backup_restore()
        assert result["blob_manifest_key"] == "qdrant/2026-id/manifest.json"
        assert result["blob_manifest_key"] in uploaded_blobs
    pod = next(item for item in applied if item["kind"] == "Pod")
    service = next(item for item in applied if item["kind"] == "Service")
    assert pod["metadata"]["labels"]["app"] != "qdrant"
    assert service["spec"]["selector"]["app"] == pod["metadata"]["labels"]["app"]
    assert pod["spec"]["volumes"] == [{"name": "restore", "emptyDir": {}}]
    command = jobs[1][1]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[-2:] == ["--blob-key", "qdrant/2026-id/"]
    deletions = [args for args in calls if "delete" in args]
    assert len(deletions) == 3
    assert not any("pvc" in args or "statefulset" in args for args in deletions)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_verification_git_changes_use_separate_checkout_and_never_include_local_edits(tmp_path):
    remote, work = tmp_path / "origin.git", tmp_path / "working"
    remote.mkdir()
    git(remote, "init", "--bare")
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.invalid")
    (work / "prompt.txt").write_text("original\n")
    (work / "unrelated.txt").write_text("original\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "initial")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    (work / "unrelated.txt").write_text("uncommitted user edit\n")
    value = collector(tmp_path)
    value.d.root = work
    value.d.config = {"git_branch": "main"}

    def transform(checkout):
        path = checkout / "prompt.txt"
        path.write_text("verification marker\n")
        return [path]

    revision = value.publish_change(transform, "verify: marker [skip ci]")
    assert git(remote, "show", revision + ":prompt.txt") == "verification marker"
    assert git(remote, "show", revision + ":unrelated.txt") == "original"
    assert (work / "unrelated.txt").read_text() == "uncommitted user edit\n"
    assert (work / "prompt.txt").read_text() == "original\n"
    assert len(git(work, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_restore_working_repository_runs_after_failed_deployment_timeout(tmp_path, monkeypatch):
    value = collector(tmp_path)
    target = tmp_path / "deploy/flux/dev/environment-values.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("metadata: {name: generation}\nspec:\n  values:\n    image: {repository: registry/generation}\n")
    revisions = []

    def publish(transform, message):
        transform(tmp_path)
        revisions.append(message)
        return "revision"

    def unavailable(*args, **kwargs):
        raise NotVerified("rollback remediation not observed")

    monkeypatch.setattr(value, "publish_change", publish)
    monkeypatch.setattr("scripts.azure_verify.await_value", unavailable)
    value.d.kube = lambda *args, **kwargs: ""
    with pytest.raises(NotVerified):
        value.failed_deployment()
    assert len(revisions) == 2
    assert "verification-missing" not in target.read_text()


@pytest.mark.parametrize("fail_before_restart", [False, True])
def test_interrupted_publication_restores_quota_and_checks_persisted_generation(tmp_path, monkeypatch, fail_before_restart):
    import base64
    import contextlib

    import httpx

    value = collector(tmp_path)
    value.file = tmp_path / "input.bin"
    value.file.write_bytes(b"original source")
    original_file = value.file
    value.base, value.tls, value.token = "https://api.invalid", True, "API-TOKEN"
    value.d.config = {"study_id": "S1"}
    value.completed_workflows = [{"index_generation": {"generation_id": "old"}}]
    old_quota = {"enabled": False, "max_resident_memory_percent": None,
                 "max_disk_usage_percent": 80, "release_margin_percent": 3}
    state = {"quota": old_quota, "uid": "before", "probe_removed": False}
    checkpoints = {"extracting": "blob:extract", "embedding": "blob:embed"}

    def handle(request):
        if request.url.host == "qdrant.invalid":
            assert request.headers.get("authorization") is None
            if request.url.path == "/quotas":
                if request.method == "PUT":
                    state["quota"] = json.loads(request.content)
                return httpx.Response(200, json={"result": {
                    "config": state["quota"], "usage": {"resident_memory_percent": 10}}})
            if request.method == "DELETE":
                state["probe_removed"] = True
            return httpx.Response(507 if request.url.path.endswith("/points") else 200, json={})
        return httpx.Response(200, json={"state": "indexing", "attempts": 1, "checkpoints": checkpoints})

    real_client = httpx.Client
    monkeypatch.setattr("scripts.azure_verify.httpx.Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))

    @contextlib.contextmanager
    def forward(*args):
        yield "http://qdrant.invalid"

    def kube(*args, **kwargs):
        if "secret" in args:
            return {"data": {"api-key": base64.b64encode(b"QDRANT-SECRET").decode()}}
        if "delete" in args:
            assert state["quota"]["enabled"] is True
            state["uid"] = "after"
        return ""

    def application(*, on_submitted):
        assert value.file.read_bytes() != original_file.read_bytes()
        on_submitted({"id": "job"})
        assert state["quota"] == old_quota
        return {"job_checkpoints": {**checkpoints, "indexing": "blob:index"},
                "index_generation": {"generation_id": "planned"}}

    value.d.kube = kube
    monkeypatch.setattr(value, "forward", forward)
    monkeypatch.setattr(value, "pods", lambda service: [{"metadata": {"name": "worker", "uid": state["uid"]}}])
    monkeypatch.setattr(value, "application", application)
    monkeypatch.setattr(value, "publication_state", lambda job: {
        "active": "changed-unsafely" if fail_before_restart else "old", "plan": {"generation_id": "planned"}})
    if fail_before_restart:
        with pytest.raises(AssertionError, match="active generation"):
            value.worker_recovery()
    else:
        result = value.worker_recovery()
        assert result["recovery"]["new_uids"] == ["after"]
        assert result["recovery"]["checkpoints_before"] == checkpoints
    assert state["quota"] == old_quota
    assert state["probe_removed"] is True
    assert value.file == original_file
    assert original_file.read_bytes() == b"original source"


def test_negative_api_checks_keep_jwt_out_of_blob_capability_request(tmp_path, monkeypatch):
    import httpx

    value = collector(tmp_path)
    value.base, value.tls, value.token = "https://api.invalid", True, "API-TOKEN"
    value.d.config = {"study_id": "S1", "section_path": "11.2"}
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "blob.invalid":
            assert "authorization" not in request.headers
            return httpx.Response(201)
        if "authorization" not in request.headers:
            return httpx.Response(401)
        if "DENIED-" in request.url.path:
            return httpx.Response(403)
        if request.url.path.endswith("/draft"):
            return httpx.Response(422)
        if request.url.path.endswith("documents:upload-url"):
            return httpx.Response(201, json={"upload_url": "https://blob.invalid/staging?sig=SECRET",
                "headers": {"x-ms-blob-type": "BlockBlob"}, "doc_id": "doc", "upload_id": "upload"})
        return httpx.Response(409 if request.method == "POST" else 404)

    real_client = httpx.Client
    monkeypatch.setattr("scripts.azure_verify.httpx.Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    result = value.negative_access()
    assert len(result) == 6
    assert "SECRET" not in json.dumps(result)
    assert any(request.url.host == "blob.invalid" for request in requests)


def test_quota_recovery_preserves_concurrent_configuration_and_removes_own_probe(tmp_path):
    import httpx

    value = collector(tmp_path)
    state = {"quota": {"enabled": False, "max_resident_memory_percent": None}, "deleted": False}

    def handle(request):
        if request.url.path == "/quotas":
            if request.method == "PUT":
                state["quota"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"config": state["quota"],
                "usage": {"resident_memory_percent": 20}}})
        if request.method == "DELETE":
            state["deleted"] = True
        return httpx.Response(507 if request.url.path.endswith("/points") else 200)

    concurrent = {"enabled": True, "max_resident_memory_percent": 50}
    with (httpx.Client(base_url="http://qdrant", transport=httpx.MockTransport(handle)) as client,
          pytest.raises(RuntimeError, match="changed concurrently"), value.quota_outage(client)):
        state["quota"] = concurrent
    assert state["quota"] == concurrent
    assert state["deleted"] is True


@pytest.mark.parametrize("sas_status", [403, 201])
def test_expired_real_registration_restores_git_configuration_after_failure(tmp_path, monkeypatch, sas_status):
    import httpx
    import yaml

    value = collector(tmp_path)
    value.base, value.tls, value.token = "https://api.invalid", True, "API-TOKEN"
    value.d.config = {"study_id": "S1"}
    target = tmp_path / "deploy/flux/dev/environment-values.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("metadata: {name: gateway}\nspec:\n  values:\n    config: {existing: retained}\n")
    waits, revisions = [], []

    def publish(transform, message):
        transform(tmp_path)
        revisions.append(message)
        return "revision"

    def handle(request):
        if request.url.host == "blob.invalid":
            assert "authorization" not in request.headers
            return httpx.Response(sas_status)
        if request.url.path.endswith("documents:upload-url"):
            assert yaml.safe_load(target.read_text())["spec"]["values"]["config"]["upload_ttl_seconds"] == 5
            return httpx.Response(201, json={"upload_url": "https://blob.invalid/staging?sig=SECRET",
                "headers": {}, "doc_id": "doc", "upload_id": "registered", "expires_at": 1005})
        return httpx.Response(403)

    def settled(check, **kwargs):
        assert check()
        waits.append(kwargs["label"])
        return True

    def kube(*args, **kwargs):
        document = yaml.safe_load(target.read_text())
        return {**document, "metadata": {"generation": 7}, "status": {"conditions": [{
            "type": "Ready", "status": "True", "observedGeneration": 7}]}}

    value.d.kube = kube
    real_client = httpx.Client
    monkeypatch.setattr("scripts.azure_verify.httpx.Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(value, "publish_change", publish)
    monkeypatch.setattr("scripts.azure_verify.await_value", settled)
    monkeypatch.setattr("scripts.azure_verify.time.time", lambda: 1000)
    monkeypatch.setattr("scripts.azure_verify.time.sleep", lambda duration: waits.append(duration))
    if sas_status == 403:
        result = value.expired_upload()
        assert result["issued_upload_id"] == "registered"
        assert "SECRET" not in json.dumps(result)
    else:
        with pytest.raises(AssertionError, match="not both rejected"):
            value.expired_upload()
    assert 7 in waits
    assert len(revisions) == 2
    assert yaml.safe_load(target.read_text())["spec"]["values"]["config"] == {"existing": "retained"}
    assert "timeout:" not in target.read_text()


def test_old_helm_remediation_and_ready_conditions_do_not_satisfy_current_revision(tmp_path, monkeypatch):
    value = collector(tmp_path)
    target = tmp_path / "deploy/flux/dev/environment-values.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("metadata: {name: generation}\nspec:\n  values:\n    image: {repository: registry/generation}\n")
    observed = {"bad": 1, "restored": 2}

    def publish(transform, message):
        transform(tmp_path)
        return "revision"

    def kube(*args, **kwargs):
        import yaml
        document = yaml.safe_load(target.read_text())
        broken = "verification-missing" in document["spec"]["values"]["image"]["repository"]
        return {**document, "metadata": {"generation": 2 if broken else 3}, "status": {"conditions": [{
            "type": "Remediated" if broken else "Ready", "status": "True", "reason": "RollbackSucceeded",
            "observedGeneration": observed["bad" if broken else "restored"]}]}}

    def wait(check, **kwargs):
        assert not check(), "a condition for a previous Helm generation must not pass"
        if "remediation" in kwargs["label"]:
            observed["bad"] = 2
        else:
            observed["restored"] = 3
        result = check()
        assert result
        return result

    value.d.kube = kube
    monkeypatch.setattr(value, "publish_change", publish)
    monkeypatch.setattr("scripts.azure_verify.await_value", wait)
    report = value.failed_deployment()
    assert report["observed_helm_status"]["conditions"][0]["observedGeneration"] == 2
    assert "verification-missing" not in target.read_text()

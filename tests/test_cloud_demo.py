"""Cloud walkthrough orchestration must retain failures and clean up its client."""
import base64
import json
from types import SimpleNamespace

import pytest

from scripts.cloud_demo import PREFIX, run_cloud


@pytest.mark.parametrize("outcome", ["success", "client_failure", "dag_failure", "wrong_batch"])
def test_cloud_walkthrough_requires_actual_dag_success_and_cleans_client(tmp_path, monkeypatch, outcome):
    source = tmp_path / "document.bin"
    source.write_bytes(b"\x00uploaded binary\xff")
    output = tmp_path / "evidence.json"
    created = {}
    removed = []
    triggered = []

    def command(arguments, *, input=None, capture_output, check):
        parts = arguments[3:]  # kubectl -n medw
        if parts[:2] == ["get", "helmrelease"]:
            response = {"spec": {"values": {"image": {"repository": "registry/ingestion-worker",
                        "digest": "sha256:" + "a" * 64, "sourceSha": "b" * 40}}}}
        elif parts[0] == "create":
            created.update(json.loads(input))
            container = created["spec"]["template"]["spec"]["containers"][0]
            assert container["image"].endswith("@sha256:" + "a" * 64)
            assert created["spec"]["template"]["spec"]["serviceAccountName"] == "demo-client"
            assert all("valueFrom" in e or e["name"] == "PYTHONDONTWRITEBYTECODE" for e in container["env"])
            response = {}
        elif parts[:2] == ["get", "pods"]:
            response = {"items": [{"metadata": {"name": "client-pod"}, "status": {
                "conditions": [{"type": "Ready", "status": "True"}]}}]}
        elif parts[:2] == ["exec", "-i"]:
            files = json.loads(input)
            assert base64.b64decode(files[0]["data"]) == source.read_bytes()
            response = {}
        elif parts[0] == "logs":
            events = [{"stage": "awaiting_batch", "jobs": ["one", "two"]}]
            if outcome != "client_failure":
                events += [{"stage": "batch_completed", "batch": {"run_id": (
                    "wrong" if outcome == "wrong_batch" else created["metadata"]["name"])}},
                    {"stage": "complete", "evidence": [{"source_revision": "uploaded-source"}]}]
            return SimpleNamespace(returncode=0, stdout=("\n".join(PREFIX + json.dumps(e) for e in events)).encode())
        elif parts[:2] == ["get", "job"]:
            response = {"status": {"failed" if outcome == "client_failure" else "succeeded": 1}}
        elif parts[0] == "exec" and "trigger" in parts:
            triggered.append(parts[-1])
            response = {}
        elif parts[0] == "exec":
            response = {"state": "failed" if outcome == "dag_failure" else "success"}
        elif parts[0] == "delete":
            removed.append(parts[2])
            response = {}
        else:
            pytest.fail(str(parts))
        return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())

    monkeypatch.setattr("scripts.cloud_demo.subprocess.run", command)
    args = SimpleNamespace(kubeconfig=None, file=[source], output=output, processing="nightly")
    if outcome == "success":
        run_cloud(args)
    else:
        with pytest.raises((RuntimeError, AssertionError)):
            run_cloud(args)
    evidence = json.loads(output.read_text())
    assert evidence["passed"] is (outcome == "success")
    assert evidence["client_job_removed"] and removed == [created["metadata"]["name"]]
    assert triggered == removed
    if outcome == "success":
        assert evidence["dag_state"] == "success"
    assert "uploaded binary" not in output.read_text()

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

MODULE = Path(__file__).resolve().parents[1] / "deploy/charts/qdrant/files/qdrant_backup.py"
spec = importlib.util.spec_from_file_location("qdrant_backup", MODULE)
assert spec and spec.loader
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def archive(tmp_path):
    data = b"synthetic snapshot"
    (tmp_path / "0-0.snapshot").write_bytes(data)
    manifest = {"format": 1, "id": "synthetic", "peer_count": 1,
                "collections": [{"name": "synthetic", "snapshots": [
                    {"peer": 0, "file": "0-0.snapshot", "sha256": hashlib.sha256(data).hexdigest()}
                ]}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_snapshot_failure_restores_all_original_write_locks():
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.host, body))
        return httpx.Response(200, json={"result": {"write": False}})
    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(RuntimeError, match="disk full"),
          backup.frozen(client, ["http://a", "http://b"], "synthetic")):
        raise RuntimeError("disk full")
    assert calls == [("a", {"write": True, "error_message": "medw-backup:synthetic"}),
                     ("b", {"write": True, "error_message": "medw-backup:synthetic"}),
                     ("b", {"write": False}), ("a", {"write": False})]


def test_partial_lock_acquisition_releases_already_acquired_locks():
    calls = []
    def respond(request):
        calls.append((request.url.host, json.loads(request.content)))
        return httpx.Response(500 if request.url.host == "b" else 200,
                              json={"result": {"write": False}})
    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(httpx.HTTPStatusError),
          backup.frozen(client, ["http://a", "http://b"], "synthetic")):
        pytest.fail("should not enter snapshot capture")
    assert calls[-1] == ("a", {"write": False})


def test_restore_rejects_corruption_before_contacting_target(tmp_path):
    archive(tmp_path)
    (tmp_path / "0-0.snapshot").write_bytes(b"truncated")
    with pytest.raises(ValueError, match="checksum"):
        backup.restore(None, ["http://unused"], tmp_path)


class Container:
    def __init__(self, fail=False):
        self.uploads = []
        self.deleted = []
        self.fail = fail
        self.old = {"collections": [{"snapshots": [{"file": "old.snapshot"}]}]}

    def upload_blob(self, name, file, overwrite):
        assert overwrite is False
        if self.fail:
            raise RuntimeError("upload interrupted")
        self.uploads.append(name)

    def list_blobs(self, name_starts_with):
        return [SimpleNamespace(name="qdrant/old/manifest.json",
                                last_modified=datetime.now(UTC) - timedelta(days=60))]

    def download_blob(self, name):
        return SimpleNamespace(readall=lambda: json.dumps(self.old).encode())

    def delete_blob(self, name):
        self.deleted.append(name)


def test_upload_commits_manifest_last_then_expires_complete_old_sets(tmp_path):
    archive(tmp_path)
    container = Container()
    backup.upload(container, tmp_path)
    assert container.uploads == ["qdrant/synthetic/0-0.snapshot", "qdrant/synthetic/manifest.json"]
    assert container.deleted == ["qdrant/old/old.snapshot", "qdrant/old/manifest.json"]


def test_upload_failure_never_publishes_manifest_or_runs_retention(tmp_path):
    archive(tmp_path)
    container = Container(fail=True)
    with pytest.raises(RuntimeError, match="interrupted"):
        backup.upload(container, tmp_path)
    assert container.uploads == []
    assert container.deleted == []


def test_overlapping_backup_preserves_existing_lock():
    calls = []
    existing = {"write": True, "error_message": "medw-backup:previous-job"}
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"result": existing})
    with (httpx.Client(transport=httpx.MockTransport(respond)) as client,
          pytest.raises(RuntimeError, match="already write-locked"),
          backup.frozen(client, ["http://a"], "new-job")):
        pytest.fail("overlapping snapshot must not start")
    assert calls[-1] == existing


def test_backup_does_not_overwrite_a_completed_archive(tmp_path):
    manifest = archive(tmp_path)
    with pytest.raises(ValueError, match="must be empty"):
        backup.backup(None, ["http://unused"], tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest


@pytest.mark.parametrize("changed", [None, "content", "collections", "aliases"])
def test_native_single_peer_capture_verifies_entire_set_before_manifest_commit(tmp_path, changed):
    later_snapshot_captured = False

    def respond(req):
        nonlocal later_snapshot_captured
        path = req.url.path
        if path == "/":
            return httpx.Response(200, json={"version": "1.19.0"})
        if path == "/locks":
            return httpx.Response(404)
        if path == "/collections":
            names = ["a", "b"] + (["new"] if changed == "collections" and later_snapshot_captured else [])
            result = {"collections": [{"name": name} for name in names]}
        elif path == "/aliases":
            result = {"aliases": [{"alias_name": "new", "collection_name": "a"}]
                      if changed == "aliases" and later_snapshot_captured else []}
        elif path.endswith("/points/scroll"):
            modified = changed == "content" and later_snapshot_captured and path.startswith("/collections/a/")
            result = {"points": [{"id": 1, "vector": [2.0 if modified else 1.0], "payload": {}}],
                      "next_page_offset": None}
        elif path.endswith("/snapshots"):
            result = {"name": "capture.snapshot"}
        elif path.endswith("capture.snapshot"):
            if req.method == "GET":
                if path.startswith("/collections/b/"):
                    later_snapshot_captured = True
                return httpx.Response(200, content=b"native atomic snapshot bytes")
            result = True
        else:
            result = {"status": "green", "config": {}}
        return httpx.Response(200, json={"result": result})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if changed:
            with pytest.raises(ValueError, match="changed"):
                backup.backup(client, ["http://source"], tmp_path)
            assert not (tmp_path / "manifest.json").exists()
            assert not (tmp_path / "manifest.pending").exists()
        else:
            manifest = backup.backup(client, ["http://source"], tmp_path)
            assert manifest["consistency_strategy"] == "native-single-peer-verified-content"
            assert len(manifest["collections"]) == 2
            assert backup.validate_archive(tmp_path) == manifest


def test_modern_multi_peer_capture_fails_closed_and_permission_failure_is_not_a_fallback():
    with (httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404))) as client,
          pytest.raises(ValueError, match="coordinated write quiescence"),
          backup.capture_strategy(client, ["http://a", "http://b"], "proof")):
        pytest.fail("must not capture modern distributed peers without coordination")
    with (httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403))) as client,
          pytest.raises(httpx.HTTPStatusError), backup.capture_strategy(client, ["http://a"], "proof")):
        pytest.fail("a rejected freeze must not silently weaken consistency")

"""Delivery failure paths exercised without contacting a cluster or registry."""
import os
import pathlib
import shutil
import subprocess

import pytest

from scripts.migrate import batches

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_migration_batches_respect_go_and_reject_repetition():
    assert batches("SELECT 1;\nGO\nSELECT 2;\nGO -- comment\n") == ["SELECT 1;", "SELECT 2;"]
    with pytest.raises(ValueError, match="repetition"):
        batches("SELECT 1;\nGO 2\n")


@pytest.mark.skipif(not shutil.which("make"), reason="make required")
def test_make_charts_fails_on_first_render_error(tmp_path):
    fake = tmp_path / "helm"
    fake.write_text("#!/bin/sh\nexit 23\n")
    fake.chmod(0o755)
    environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["make", "charts"], cwd=ROOT, env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_stale_build_cannot_overwrite_newer_release(tmp_path):
    import yaml

    from scripts.commit_release import assert_forward

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    document = tmp_path / "example"
    document.write_text("first")
    git("add", "example")
    git("commit", "-m", "first")
    old = git("rev-parse", "HEAD")
    document.write_text("second")
    git("commit", "-am", "second")
    new = git("rev-parse", "HEAD")
    pointer = tmp_path / "deploy/flux/dev/release-values.yaml"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(yaml.safe_dump({"metadata": {"name": "gateway"}, "spec": {
        "values": {"image": {"sourceSha": new}}}}))
    with pytest.raises(ValueError, match="stale"):
        assert_forward({"images": {"gateway": {"source_sha": old}}}, "dev", tmp_path)
    assert_forward({"images": {"gateway": {"source_sha": new}}}, "dev", tmp_path)


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_release_commit_retries_and_is_idempotent_without_touching_checkout(tmp_path):
    import json
    import sys

    from scripts.release import SERVICES, create

    working = tmp_path / "working"
    remote = tmp_path / "origin.git"
    working.mkdir()

    def git(*args, cwd=working):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    git("init", "--bare", "--initial-branch=main", str(remote))
    git("init", "--initial-branch=main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (working / ".gitignore").write_text("__pycache__/\n")
    scripts = working / "scripts"
    scripts.mkdir()
    for name in ("release.py", "commit_release.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    target = working / "deploy/flux/dev"
    target.mkdir(parents=True)
    (target / "release-values.yaml").write_text(
        "kind: HelmRelease\nmetadata: {name: gateway}\nspec: {suspend: true}\n")
    git("add", ".")
    git("commit", "-m", "source")
    source = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "main")
    # Reject the first release push to exercise a fresh fetch/retry. This is
    # a disposable local bare repository; no project's remote is contacted.
    hook = remote / "hooks/pre-receive"
    hook.write_text('#!/bin/sh\nif [ ! -e "$GIT_DIR/retried" ]; then\n'
                    '  touch "$GIT_DIR/retried"\n  exit 1\nfi\nexit 0\n')
    hook.chmod(0o755)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "test.md").write_text("synthetic")
    images = {s: {"digest": "sha256:" + "1" * 64, "source_sha": source} for s in SERVICES}
    behavior = {"chat_model_name": "example", "chat_model_version": "1",
                "embed_model_name": "example", "embed_model_version": "1", "embed_version": "v1", "embed_dim": 3072}
    bundle = create(images, behavior, prompts)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "libs")}
    command = [sys.executable, str(scripts / "commit_release.py"), str(path),
               "--environment", "dev", "--branch", "main", "--push"]
    subprocess.run(command, cwd=working, env=environment, check=True, capture_output=True)
    published = git("rev-parse", "main", cwd=remote)
    assert published != source
    assert (remote / "retried").exists()
    assert git("rev-parse", "HEAD") == source
    assert git("status", "--porcelain") == ""
    subprocess.run(command, cwd=working, env=environment, check=True, capture_output=True)
    assert git("rev-parse", "main", cwd=remote) == published


def test_target_preparation_requires_identity_and_both_telemetry_paths():
    import copy

    import yaml

    from scripts.check_model_deployments import REQUIRED_TARGETS, validate_targets

    docs = list(yaml.safe_load_all((ROOT / "deploy/flux/dev/environment-values.yaml").read_text()))
    with pytest.raises(ValueError, match="target setting"):
        validate_targets(docs)
    for doc in docs:
        name = doc["metadata"]["name"]
        if name not in REQUIRED_TARGETS:
            continue
        values = doc["spec"]["values"]
        for key in REQUIRED_TARGETS[name]:
            values["config"][key] = "configured"
        values["serviceAccount"]["annotations"]["azure.workload.identity/client-id"] = "1" * 32
    validate_targets(docs)
    for field in ("serviceMonitor", "secretEnv", "serviceAccount"):
        broken = copy.deepcopy(docs)
        del broken[0]["spec"]["values"][field]
        with pytest.raises(ValueError):
            validate_targets(broken)

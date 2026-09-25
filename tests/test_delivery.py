"""Delivery failure paths exercised without contacting a cluster or registry."""
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pull_request_validation_is_separate_from_azure_delivery():
    directory = ROOT / "deploy/azure-pipelines"
    validation = yaml.safe_load((directory / "validation.yml").read_text())
    assert validation["trigger"] == "none"
    assert validation["pr"]["branches"]["include"] == ["main"]
    steps = validation["steps"]
    expanded = []
    for step in steps:
        if "template" in step:
            expanded.extend(yaml.safe_load((directory / step["template"]).read_text())["steps"])
        else:
            expanded.append(step)
    assert next(step for step in expanded if "checkout" in step)["persistCredentials"] is False
    assert not any(step.get("task", "").startswith("AzureCLI") for step in expanded)
    scripts = "\n".join(step.get("bash", "") for step in expanded)
    for command in ("make check", "make terraform-check", "make images",
                    "MEDW_SQL_IMAGE_TESTS=1", "tests/integration/test_sql_migrations.py"):
        assert command in scripts
    for command in ("publish_images.py", "commit_release.py", "scripts/migrate.sh", "terraform apply"):
        assert command not in scripts
    for name in ("delivery", "infrastructure"):
        pipeline = yaml.safe_load((directory / f"{name}.yml").read_text())
        assert pipeline["pr"] == "none"
        assert pipeline["trigger"]["branches"]["include"] == ["main"]


def test_delivery_requires_disposable_sql_check_before_publishing():
    pipeline = yaml.safe_load((ROOT / "deploy/azure-pipelines/delivery.yml").read_text())
    step = next(step for step in pipeline["steps"] if "scripts/publish_images.py" in
                step.get("inputs", {}).get("inlineScript", ""))
    script = step["inputs"]["inlineScript"]
    assert script.index("docker buildx bake") < script.index("test_sql_migrations.py")
    assert script.index("test_sql_migrations.py") < script.index("scripts/publish_images.py")
    assert 'MEDW_SQL_TEST_IMAGE="$registry/generation:$TAG"' in script
    assert "set -euo pipefail" in script
    assert "condition" not in step and not step.get("continueOnError", False)


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
    unprepared = copy.deepcopy(docs)
    gateway = next(doc for doc in unprepared if doc["metadata"]["name"] == "gateway")
    gateway["spec"]["values"]["config"]["cosmos_endpoint"] = ""
    with pytest.raises(ValueError, match="gateway: target setting cosmos_endpoint must be configured"):
        validate_targets(unprepared)
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


@pytest.mark.parametrize("fail_smoke", [False, True])
def test_publishing_requires_every_smoke_before_any_push(tmp_path, monkeypatch, fail_smoke):
    import json

    from scripts import publish_images
    output = tmp_path / "images.json"
    output.write_text('{"stale": true}')
    smoked, pushed = set(), set()
    source = "a" * 40
    def command(*args, capture=False):
        if args[:2] == ("git", "status"):
            return ""
        if args[:2] == ("git", "rev-parse"):
            return source
        if "scripts/smoke_image.py" in args:
            service = args[args.index("--service") + 1]
            if fail_smoke and service == "generation":
                raise RuntimeError("synthetic smoke failure")
            smoked.add(service)
        elif args[:2] == ("docker", "push"):
            assert smoked == set(publish_images.ARTIFACTS)
            pushed.add(args[2].split("/")[1].split(":")[0])
        elif args[:3] == ("docker", "image", "inspect"):
            if "Labels" in args[-1]:
                return source
            return json.dumps([args[3].split(":")[0] + "@sha256:" + "1" * 64])
        else:
            pytest.fail(f"Unexpected command: {args[:3]}")
        return ""
    monkeypatch.setattr(publish_images, "run", command)
    monkeypatch.setattr(publish_images.sys, "argv", ["publish_images.py", "--registry", "example.azurecr.io",
                                                   "--output", str(output)])
    if fail_smoke:
        with pytest.raises(RuntimeError, match="smoke failure"):
            publish_images.main()
        assert not output.exists() and not pushed
    else:
        publish_images.main()
        assert set(json.loads(output.read_text())) == smoked == pushed == set(publish_images.ARTIFACTS)

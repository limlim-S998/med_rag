"""Release invariants: independent environments, immutable artifacts and honest provenance."""
from __future__ import annotations

import copy
import json
import pathlib
import shutil
import subprocess

import pytest
import yaml

from scripts.release import SERVICES, content_hash, create, select, validate

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHARTS = ROOT / "deploy/charts"


@pytest.fixture
def bundle(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "draft.md").write_text("synthetic prompt")
    images = {service: {"digest": "sha256:" + str(index + 1) * 64,
                        "source_sha": "a" * 40} for index, service in enumerate(SERVICES)}
    behavior = {"chat_model_name": "example-chat", "chat_model_version": "2026-01-01",
                "embed_model_name": "example-embed", "embed_model_version": "1",
                "embed_version": "example-v1", "embed_dim": 3072, "temperature": "0"}
    return create(images, behavior, prompts)


def test_promotion_and_rollback_preserve_complete_identity(bundle, tmp_path):
    for env in ("dev", "staging", "prod"):
        target = tmp_path / "deploy/flux" / env
        target.mkdir(parents=True)
        (target / "environment-values.yaml").write_text(f"endpoint: {env}\n")
        (target / "release-values.yaml").write_text("unselected\n")
    select(bundle, "dev", root=tmp_path)
    dev = (tmp_path / "deploy/flux/dev/release-values.yaml").read_bytes()
    assert (tmp_path / "deploy/flux/staging/release-values.yaml").read_text() == "unselected\n"
    assert (tmp_path / "deploy/flux/prod/release-values.yaml").read_text() == "unselected\n"
    select(bundle, "staging", root=tmp_path)
    assert (tmp_path / "deploy/flux/staging/release-values.yaml").read_bytes() == dev
    advanced = copy.deepcopy(bundle)
    advanced["images"]["gateway"]["digest"] = "sha256:" + "f" * 64
    advanced["bundle_sha"] = content_hash({k: v for k, v in advanced.items() if k != "bundle_sha"})
    select(advanced, "dev", root=tmp_path)
    assert (tmp_path / "deploy/flux/staging/release-values.yaml").read_bytes() == dev
    select(bundle, "dev", root=tmp_path)
    assert (tmp_path / "deploy/flux/dev/release-values.yaml").read_bytes() == dev
    for env in ("dev", "staging", "prod"):
        assert (tmp_path / f"deploy/flux/{env}/environment-values.yaml").read_text() == f"endpoint: {env}\n"


@pytest.mark.parametrize("mutation", ["tag", "source", "missing", "model", "endpoint", "tamper", "zero", "floating"])
def test_invalid_release_cannot_be_selected(bundle, tmp_path, mutation):
    if mutation == "tag":
        bundle["images"]["gateway"]["digest"] = "latest"
    elif mutation == "source":
        bundle["images"]["gateway"]["source_sha"] = "PLACEHOLDER"
    elif mutation == "missing":
        del bundle["images"]["reranker"]
    elif mutation == "model":
        bundle["behavior"]["embed_model_version"] = ""
    elif mutation == "endpoint":
        bundle["behavior"]["aoai_endpoint"] = "https://prod.invalid"
    elif mutation == "zero":
        bundle["images"]["gateway"]["digest"] = "sha256:" + "0" * 64
    elif mutation == "floating":
        bundle["behavior"]["embed_model_version"] = "latest"
    else:
        bundle["behavior"]["temperature"] = "1"
    with pytest.raises(ValueError):
        select(bundle, "dev", root=tmp_path)
    assert not list(tmp_path.rglob("release-values.yaml"))


def test_prompt_content_changes_release_identity(bundle, tmp_path):
    prompts = tmp_path / "changed"
    prompts.mkdir()
    (prompts / "draft.md").write_text("changed prompt")
    changed = create(bundle["images"], bundle["behavior"], prompts)
    assert changed["behavior"]["prompt_bundle_sha"] != bundle["behavior"]["prompt_bundle_sha"]
    assert changed["bundle_sha"] != bundle["bundle_sha"]
    assert validate(json.loads(json.dumps(changed))) == changed


@pytest.mark.parametrize("service", SERVICES)
def test_library_pin_and_lock_match_source(service):
    library = yaml.safe_load((CHARTS / "medw-lib/Chart.yaml").read_text())["version"]
    chart = yaml.safe_load((CHARTS / service / "Chart.yaml").read_text())
    lock = yaml.safe_load((CHARTS / service / "Chart.lock").read_text())
    assert chart["dependencies"][0]["version"] == library
    assert lock["dependencies"][0]["version"] == library


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl is required for delivery checks")
@pytest.mark.parametrize("environment", ["base", "dev", "staging", "prod", "local"])
def test_rendered_flux_sources_and_chart_revision_strategy(environment):
    rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "deploy/flux" / environment)], text=True)
    docs = list(yaml.safe_load_all(rendered))
    sources = {(d["metadata"].get("namespace", "default"), d["metadata"]["name"])
               for d in docs if d["kind"] == "GitRepository"}
    for doc in docs:
        if doc["kind"] != "HelmRelease":
            continue
        spec = doc["spec"]["chart"]["spec"]
        assert spec["reconcileStrategy"] == "Revision"
        ref = spec["sourceRef"]
        assert (ref["namespace"], ref["name"]) in sources


def test_generation_promotion_depends_on_successful_migration():
    pipeline = yaml.safe_load((ROOT / "deploy/azure-pipelines/_build-template.yml").read_text())
    stages = {s["stage"]: s for s in pipeline["stages"]}
    assert stages["validate_models"]["dependsOn"] == "build"
    assert stages["migrate"]["dependsOn"] == "validate_models"
    assert stages["promote_to_dev"]["dependsOn"] == "migrate"
    assert stages["promote_to_dev"]["condition"] == "succeeded()"


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl required")
def test_selected_release_pins_chart_source_without_changing_other_environments(bundle, tmp_path):
    working = tmp_path / "checkout"
    shutil.copytree(ROOT / "deploy/flux", working / "deploy/flux")
    staging_before = (working / "deploy/flux/staging/release-values.yaml").read_bytes()
    select(bundle, "dev", root=working)
    rendered = subprocess.check_output(["kubectl", "kustomize", str(working / "deploy/flux/dev")], text=True)
    docs = list(yaml.safe_load_all(rendered))
    source = next(d for d in docs if d["kind"] == "GitRepository"
                  and d["metadata"]["name"] == "medwriter-release-charts")
    assert source["spec"]["ref"] == {"commit": bundle["chart_source_sha"]}
    assert source["spec"]["suspend"] is False
    for release in (d for d in docs if d["kind"] == "HelmRelease"):
        assert release["spec"]["chart"]["spec"]["sourceRef"]["name"] == "medwriter-release-charts"
        name = release["metadata"]["name"]
        if name in SERVICES:
            assert release["spec"]["values"]["image"]["digest"] == bundle["images"][name]["digest"]
            assert release["spec"]["suspend"] is False
    assert (working / "deploy/flux/staging/release-values.yaml").read_bytes() == staging_before


def test_chart_only_release_preserves_image_and_behavior_identity(bundle):
    changed = copy.deepcopy(bundle)
    changed["chart_source_sha"] = "b" * 40
    changed["bundle_sha"] = content_hash({k: v for k, v in changed.items() if k != "bundle_sha"})
    validate(changed)
    assert changed["images"] == bundle["images"]
    assert changed["behavior"] == bundle["behavior"]
    assert changed["bundle_sha"] != bundle["bundle_sha"]


def test_azure_delivery_checks_charts_before_publishing():
    pipeline = yaml.safe_load((ROOT / "deploy/azure-pipelines/_build-template.yml").read_text())
    steps = pipeline["stages"][0]["jobs"][0]["steps"]
    assert any(step.get("task") == "HelmInstaller@1" for step in steps)
    assert any(step.get("task") == "KubectlInstaller@0" for step in steps)
    checks = next(i for i, step in enumerate(steps) if "make lint arch types test charts" in step.get("script", ""))
    publish = next(i for i, step in enumerate(steps) if "scripts/build_images.py" in step.get("script", ""))
    assert checks < publish


@pytest.mark.parametrize("dimension", [None, 0, -1, True, "3072"])
def test_release_requires_explicit_positive_embedding_dimensions(bundle, dimension):
    bundle["behavior"]["embed_dim"] = dimension
    with pytest.raises(ValueError, match="embed_dim"):
        validate(bundle)


@pytest.mark.skipif(not shutil.which("helm"), reason="Helm required for effective values")
def test_cloud_rendering_has_no_historical_endpoint_fallbacks(tmp_path):
    targets = {"MEDW_AOAI_ENDPOINT", "MEDW_AOAI_RESOURCE_ID", "MEDW_COSMOS_ENDPOINT",
               "MEDW_BLOB_ACCOUNT_URL", "MEDW_SEARCH_ENDPOINT", "MEDW_DOCINTEL_ENDPOINT",
               "MEDW_LANGUAGE_ENDPOINT", "MEDW_SQL_SERVER", "MEDW_AUTH_TENANT_ID",
               "MEDW_AUTH_AUDIENCE", "MEDW_AUTH_ISSUER", "MEDW_AUTH_JWKS_URL"}
    for environment in ("dev", "staging", "prod"):
        documents = yaml.safe_load_all((ROOT / f"deploy/flux/{environment}/environment-values.yaml").read_text())
        for document in documents:
            service = document["metadata"]["name"]
            if service not in SERVICES:
                continue
            path = tmp_path / "values.yaml"
            path.write_text(yaml.safe_dump(document["spec"]["values"]))
            rendered = subprocess.check_output([
                "helm", "template", service, str(CHARTS / service), "-f", str(path)
            ], text=True)
            deployment = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "Deployment")
            variables = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
            for variable in variables:
                if variable["name"] in targets:
                    assert variable["value"] == "", (environment, service, variable)
            telemetry = next(v for v in variables if v["name"] == "MEDW_APPINSIGHTS_CONNECTION_STRING")
            assert telemetry["valueFrom"]["secretKeyRef"] == {
                "name": "medw-telemetry", "key": "connection-string"}

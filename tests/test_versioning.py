"""Release invariants: independent environments, immutable artifacts and honest provenance."""
from __future__ import annotations

import copy
import json
import pathlib
import shutil
import subprocess

import pytest
import yaml

from scripts.release import SERVICES, content_hash, create, release_patches, select, validate

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
@pytest.mark.parametrize("environment", ["base", "dev", "staging", "prod"])
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
    pipeline = yaml.safe_load((ROOT / "deploy/azure-pipelines/delivery.yml").read_text())
    steps = pipeline["steps"]
    migrate = next(i for i, step in enumerate(steps) if "scripts/migrate.sh" in step.get("inputs", {}).get("inlineScript", ""))
    promote = next(i for i, step in enumerate(steps) if "scripts/commit_release.py" in step.get("bash", ""))
    assert migrate < promote
    assert "condition" not in steps[promote] # Azure's default requires prior steps to succeed.
    assert "continueOnError" not in steps[migrate]


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl required")
@pytest.mark.parametrize("selected_schema_version", [1, 2])
def test_selected_release_pins_chart_source_without_changing_other_environments(bundle, tmp_path,
                                                                               selected_schema_version):
    historical = bundle
    bundle = create({**bundle["images"], "airflow": {
        "digest": "sha256:" + "6" * 64, "source_sha": "a" * 40}}, bundle["behavior"], tmp_path / "prompts")
    working = tmp_path / "checkout"
    shutil.copytree(ROOT / "deploy/flux", working / "deploy/flux")
    # The checked-in selection changes after deployment. Establish each starting
    # contract explicitly, then promote a complete release including Airflow.
    existing = historical if selected_schema_version == 1 else bundle
    (working / "deploy/flux/dev/release-values.yaml").write_text(
        yaml.safe_dump_all(release_patches(existing), sort_keys=False))
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
        elif name == "airflow":
            assert release["spec"]["values"]["airflow"]["images"]["airflow"]["digest"] == bundle["images"][name]["digest"]
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


def test_airflow_release_is_atomic_and_older_contract_cannot_replace_it(bundle, tmp_path):
    old = copy.deepcopy(bundle)
    bundle["schema_version"] = 2
    bundle["images"]["airflow"] = {"digest": "sha256:" + "6" * 64, "source_sha": "a" * 40}
    bundle["bundle_sha"] = content_hash({k: v for k, v in bundle.items() if k != "bundle_sha"})
    validate(bundle)
    target = tmp_path / "deploy/flux/dev"
    target.mkdir(parents=True)
    select(old, "dev", root=tmp_path)
    select(bundle, "dev", root=tmp_path)
    selected = (target / "release-values.yaml").read_bytes()
    with pytest.raises(ValueError, match="schema-2 rollback"):
        select(old, "dev", root=tmp_path)
    assert (target / "release-values.yaml").read_bytes() == selected
    changed = copy.deepcopy(bundle)
    changed["chart_source_sha"] = "b" * 40
    changed["bundle_sha"] = content_hash({k: v for k, v in changed.items() if k != "bundle_sha"})
    names = []
    for release in (bundle, changed):
        airflow = next(p["spec"] for p in release_patches(release) if p["metadata"]["name"] == "airflow")
        assert airflow["suspend"] is False
        assert airflow["values"]["airflow"]["images"]["airflow"]["digest"] == release["images"]["airflow"]["digest"]
        names.append([json.loads(p["patch"])[0]["value"] for p in
                      airflow["postRenderers"][0]["kustomize"]["patches"]])
    assert set(names[0]).isdisjoint(names[1])  # Chart-only upgrades must recreate immutable Jobs too.
    select(changed, "dev", root=tmp_path)
    select(bundle, "dev", root=tmp_path)  # Supported rollback preserves all six artifacts.
    assert (target / "release-values.yaml").read_bytes() == selected
    del bundle["images"]["airflow"]
    with pytest.raises(ValueError, match="artifacts"):
        validate(bundle)


@pytest.mark.skipif(not shutil.which("helm"), reason="Helm required")
def test_airflow_chart_runs_pinned_private_scheduler_with_persistent_metadata(tmp_path):
    values = {"releaseRequired": True, "image": {"digest": "sha256:" + "6" * 64, "sourceSha": "a" * 40},
              "config": {"release_bundle_sha": "sha256:" + "b" * 64},
              "airflow": {"images": {"airflow": {"digest": "sha256:" + "6" * 64}}}}
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    command = ["helm", "template", "airflow", str(CHARTS / "airflow"), "-f", str(path)]
    rendered = subprocess.check_output(command, text=True)
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    scheduler = next(d for d in docs if d["metadata"]["name"] == "airflow-scheduler"
                     and d["kind"] == "StatefulSet")
    pod = scheduler["spec"]["template"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert pod["metadata"]["labels"]["medw-component"] == "airflow"
    assert pod["spec"]["serviceAccountName"] == "airflow"
    assert pod["spec"]["containers"][0]["image"].endswith("@sha256:" + "6" * 64)
    database = next(d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "airflow-db")
    assert database["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "4Gi"
    assert database["spec"]["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Retain"
    assert not any(d["kind"] in ("Ingress", "VirtualServer") for d in docs)
    assert not any("redis" in d["metadata"]["name"] for d in docs)
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "airflow-config")
    assert "executor = LocalExecutor" in config["data"]["airflow.cfg"]
    jobs = [d for d in docs if d["kind"] == "Job"]
    assert len(jobs) == 2
    for job in jobs:
        assert "helm.sh/hook" not in job["metadata"].get("annotations", {})
        variables = job["spec"]["template"]["spec"]["containers"][0]["env"]
        assert not any(v["name"] == "MEDW_BATCH_SCHEDULE" for v in variables)
    creator = next(d for d in jobs if d["metadata"]["name"] == "airflow-create-user")
    container = creator["spec"]["template"]["spec"]["containers"][0]
    assert "check-migrations" in container["args"][-1]
    password = next(v for v in container["env"] if v["name"] == "AIRFLOW_ADMIN_PASSWORD")
    assert password["valueFrom"]["secretKeyRef"] == {"name": "medw-airflow", "key": "admin-password"}
    values["airflow"]["images"]["airflow"]["digest"] = "sha256:" + "7" * 64
    path.write_text(yaml.safe_dump(values))
    failed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert failed.returncode != 0 and "match the selected release" in failed.stderr


def test_azure_delivery_checks_charts_before_publishing():
    pipeline = yaml.safe_load((ROOT / "deploy/azure-pipelines/delivery.yml").read_text())
    steps = pipeline["steps"]
    assert any(step.get("task") == "HelmInstaller@1" for step in steps)
    assert any(step.get("task") == "KubectlInstaller@0" for step in steps)
    checks = next(i for i, step in enumerate(steps) if "make check" in step.get("bash", ""))
    publish = next(i for i, step in enumerate(steps) if "scripts/publish_images.py" in
                   step.get("inputs", {}).get("inlineScript", step.get("script", "")))
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
            # A commissioned environment contains real references. Exercise
            # missing configuration explicitly, without depending on that state.
            values = document["spec"]["values"]
            for variable in targets:
                values.get("config", {}).pop(variable.removeprefix("MEDW_").lower(), None)
            path = tmp_path / "values.yaml"
            path.write_text(yaml.safe_dump(values))
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

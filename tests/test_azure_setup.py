"""Cloud-independent deployment safety and generated configuration contracts."""
import copy
import json
from pathlib import Path

import pytest

from scripts import azure

ROOT = Path(__file__).resolve().parents[1]


def config():
    return azure.load_config(ROOT / "infra/azure.example.json")


def test_unknown_quota_is_not_treated_as_available():
    assert not azure.quota_available([], "cores")
    assert not azure.quota_available([{"name": {"value": "cores"}, "limit": 4,
                                       "currentValue": 1}], "cores")
    assert azure.quota_available([{"name": {"value": "cores"}, "limit": 8,
                                   "currentValue": 4}], "cores")


def test_permissions_account_for_not_actions_and_combined_roles():
    entries = [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*"]}]
    assert not azure.permission_allows(entries, "Microsoft.Authorization/roleAssignments/write")
    assert azure.permission_allows(entries, "Microsoft.Storage/storageAccounts/write")
    entries.append({"actions": ["Microsoft.Authorization/roleAssignments/*"]})
    assert azure.permission_allows(entries, "Microsoft.Authorization/roleAssignments/write")


def test_preflight_failure_never_provisions(monkeypatch, tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    monkeypatch.setattr(deployment, "preflight", lambda: {
        "passed": False, "checks": [{"name": "quota", "passed": False, "detail": "unknown"}]})
    monkeypatch.setattr(deployment, "_resources", lambda: pytest.fail("must not create resources"))
    with pytest.raises(azure.SetupError, match="before paid creation"):
        deployment.up()
    assert json.loads((deployment.directory / "preflight.json").read_text())["passed"] is False


def test_cost_reserves_usage_and_both_os_and_data_disks():
    quote = {kind: {"hourly_aud": 0.1} for kind in
             ("node", "registry", "sql", "disk", "load_balancer", "public_ip")}
    result = azure.estimate_cost(quote, 4)
    assert result["estimated_aud"] == 8.2
    assert result["is_hard_cap"] is False


def test_checkpoint_reuses_success_and_records_interrupted_work(tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    assert deployment.checkpoint("resource", lambda: {"id": "owned"}) == {"id": "owned"}
    deployment = azure.Deployment(config(), root=tmp_path)
    assert deployment.checkpoint("resource", lambda: pytest.fail("duplicate creation")) == {"id": "owned"}
    with pytest.raises(azure.SetupError):
        deployment.checkpoint("interrupted", lambda: (_ for _ in ()).throw(azure.SetupError("network")))
    assert "interrupted" in json.loads(deployment.journal_path.read_text())["pending"]


def test_existing_untagged_group_cannot_be_adopted(monkeypatch, tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.config["owner"] = "ours"
    monkeypatch.setattr(azure, "az", lambda *args, **kwargs: {"id": "existing", "tags": {}})
    with pytest.raises(azure.SetupError, match="Refusing to adopt"):
        deployment._owned_group()


def test_cleanup_requires_recorded_ownership(tmp_path, monkeypatch):
    deployment = azure.Deployment(config(), root=tmp_path)
    monkeypatch.setattr(azure, "az", lambda *args, **kwargs: pytest.fail("must not delete resources"))
    with pytest.raises(azure.SetupError, match="No ownership journal"):
        deployment.down()


def test_environment_generation_uses_real_backends_and_bounded_resources(tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.root = ROOT
    deployment.config["tenant_id"] = "tenant"
    deployment.state = {"resources": {"blob_url": "https://blobs.example", "cosmos_url": "https://cosmos.example",
        "search_url": "https://search.example", "sql_server": "sql.example", "registry_host": "acr.example"},
        "applications": {"api": {"appId": "client"}}, "hostname": "writer.example",
        "identities": {service: {"clientId": service + "-id"} for service in (*azure.SERVICES, "qdrant-backup")}}
    manifests = deployment.environment_values()
    for manifest in manifests:
        name, values = manifest["metadata"]["name"], manifest["spec"]["values"]
        if name == "qdrant":
            assert values["replicas"] == 1
            assert values["config"]["write_consistency_factor"] == 1
        else:
            assert values["config"]["backend"] == "azure"
            assert values["autoscaling"]["maxReplicas"] == 2
            assert "demo_mode" not in values["config"]
            assert not values["config"].get("aoai_endpoint")


def test_invalid_borrowed_resource_type_is_rejected():
    with pytest.raises(azure.SetupError):
        azure.arm_parts("/subscriptions/sub/resourceGroups/group/providers/Microsoft.Sql/servers/db",
                        "Microsoft.Search", "searchServices")


def test_configuration_rejects_paid_budget_fallback(tmp_path):
    invalid = copy.deepcopy(config())
    invalid["budget_aud"] = 100
    path = tmp_path / "config.json"
    path.write_text(json.dumps(invalid))
    with pytest.raises(azure.SetupError, match="budget_aud"):
        azure.load_config(path)


def test_changed_borrowed_resource_cannot_reuse_ownership(tmp_path):
    original = config()
    deployment = azure.Deployment(original, root=tmp_path)
    deployment.state = {"config": original, "completed": {"search-index": {"name": "owned"}}}
    deployment.save()
    changed = copy.deepcopy(original)
    changed["search_index"] = "unrelated-existing-index"
    with pytest.raises(azure.SetupError, match="differs in search_index"):
        azure.Deployment(changed, root=tmp_path)


def test_borrowed_resources_use_their_own_subscription(monkeypatch, tmp_path):
    import io
    cfg = config()
    cfg.update(subscription_id="main-sub", tenant_id="same-tenant",
               borrowed_search_id="/subscriptions/search-sub/resourceGroups/shared/providers/Microsoft.Search/searchServices/free-search",
               borrowed_cosmos_id="/subscriptions/cosmos-sub/resourceGroups/shared/providers/Microsoft.DocumentDB/databaseAccounts/free-cosmos")
    calls = []

    def fake_az(*args, **kwargs):
        calls.append((args, kwargs.get("subscription")))
        if args[:2] == ("account", "show"):
            return {"tenantId": "same-tenant"}
        if args[:3] == ("search", "service", "show"):
            return {"sku": {"name": "free"}, "authOptions": {"aadOrApiKey": {}}}
        if args[:3] == ("search", "admin-key", "show"):
            return {"primaryKey": "unused-test-secret"}
        if args[:2] == ("cosmosdb", "show"):
            return {"kind": "GlobalDocumentDB", "enableFreeTier": True, "locations": [{}]}
        if args[:4] == ("cosmosdb", "sql", "database", "list"):
            return []
        pytest.fail(f"Unexpected Azure call: {args}")

    monkeypatch.setattr(azure, "az", fake_az)
    monkeypatch.setattr(azure.urllib.request, "urlopen", lambda *args, **kwargs:
                        io.BytesIO(b'{"value": []}'))
    azure.Deployment(cfg, root=tmp_path)._borrowed()
    assert all(subscription == "search-sub" for args, subscription in calls if args[0] == "search")
    assert all(subscription == "cosmos-sub" for args, subscription in calls if args[0] == "cosmosdb")


@pytest.mark.parametrize(("path", "expected"), [
    ("pipelines/4/runs/7?api-version=7.1", {"pipelineId=4", "runId=7"}),
    ("pipelines/pipelinepermissions/endpoint/connection?api-version=7.1-preview.1",
     {"resourceType=endpoint", "resourceId=connection"}),
])
def test_devops_routes_preserve_pipeline_and_resource_identifiers(monkeypatch, tmp_path, path, expected):
    cfg = config()
    cfg["devops"].update(organization="https://dev.azure.com/example", project="project")
    commands = []
    monkeypatch.setattr(azure, "run", lambda args, **kwargs: commands.append(args) or {})
    azure.Deployment(cfg, root=tmp_path).devops(path)
    assert expected <= set(commands[0])
    assert "7.1-preview.1" not in commands[0]  # CLI parses preview suffix numerically.


def test_rbac_retry_does_not_hide_unrelated_errors(monkeypatch):
    monkeypatch.setattr(azure.time, "sleep", lambda seconds: pytest.fail("must not retry"))
    with pytest.raises(azure.SetupError, match="BadRequest"):
        azure.retry_rbac(lambda: (_ for _ in ()).throw(azure.SetupError("BadRequest")))


def devops_check(monkeypatch, tmp_path):
    cfg = config()
    cfg["devops"].update(organization="https://dev.azure.com/example", project="project",
                         github_service_connection_id="github")
    deployment = azure.Deployment(cfg, root=tmp_path)
    deployment.root = ROOT

    def api(path, **kwargs):
        if path.startswith("serviceendpoint/"):
            return {"type": "github", "isReady": True}
        return {"value": []}

    monkeypatch.setattr(deployment, "devops", api)
    return deployment


def test_preflight_automatically_probes_missing_build_capacity(monkeypatch, tmp_path):
    deployment = devops_check(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(deployment, "probe_build", lambda: calls.append("probe"))
    deployment._devops_preflight()
    assert calls == ["probe"]


def test_preflight_reuses_matching_fresh_probe(monkeypatch, tmp_path):
    deployment = devops_check(monkeypatch, tmp_path)
    deployment.state["capacity_probe"] = {
        "passed": True, "agent_pool": "", "timestamp": azure.dt.datetime.now(azure.dt.UTC).isoformat(),
        "probe_sha256": azure.hashlib.sha256((ROOT / "deploy/azure-pipelines/preflight.yml").read_bytes()).hexdigest()}
    monkeypatch.setattr(deployment, "probe_build", lambda: pytest.fail("unnecessary CI work"))
    deployment._devops_preflight()


def test_preflight_does_not_probe_incomplete_devops_configuration(monkeypatch, tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    monkeypatch.setattr(deployment, "probe_build", lambda: pytest.fail("must not queue CI"))
    with pytest.raises(azure.SetupError, match="organization"):
        deployment._devops_preflight()

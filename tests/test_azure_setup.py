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


def test_quota_after_teardown_checks_new_capacity_instead_of_deleted_cluster(tmp_path, monkeypatch):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.state = {"teardown_complete": True, "completed": {"aks": {"id": "deleted-cluster"}}}

    def query(*args):
        if args[:2] == ("vm", "list-usage"):
            return [{"name": {"value": name}, "limit": 4, "currentValue": 0}
                    for name in ("cores", "standardDSv5Family")]
        assert args[:2] == ("vm", "list-skus")
        return [{"restrictions": []}]

    monkeypatch.setattr(azure, "az", query)
    assert "quota covers 4 vCPUs" in deployment._quota()


def test_cloud_client_configuration_contains_public_trust_and_scoped_identity(tmp_path, monkeypatch):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.state = {"hostname": "api.example", "identities": {"demo-client": {"clientId": "client"}},
                        "applications": {"api": {"appId": "audience"}}}
    objects = []
    monkeypatch.setattr(deployment, "apply", lambda *args: objects.extend(args))
    deployment._demo_client("PUBLIC CERTIFICATE")
    assert {o["kind"] for o in objects} == {"ServiceAccount", "ConfigMap", "NetworkPolicy"}
    public = next(o for o in objects if o["kind"] == "ConfigMap")["data"]
    assert public["ca.pem"] == "PUBLIC CERTIFICATE"
    assert public["base-url"] == "https://api.example" and public["study-id"] == config()["study_id"]
    policy = next(o for o in objects if o["kind"] == "NetworkPolicy")["spec"]
    assert not policy["ingress"]
    assert all(p["port"] in (53, 443) for rule in policy["egress"] for p in rule["ports"])


def test_platform_helm_reruns_preserve_aks_owned_fields_without_force():
    assert azure.helm_apply_options("v3.19.0+abc") == []
    assert azure.helm_apply_options("v4.3.0+bec5b06") == ["--server-side=false"]
    with pytest.raises(azure.SetupError, match="Helm major"):
        azure.helm_apply_options("unknown")
    with pytest.raises(azure.SetupError, match=r"Helm 3\.19"):
        azure.helm_apply_options("v3.18.5")


def test_delivery_sql_sid_uses_client_guid_and_repairs_only_mismatches(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import azure_sql
    statements = []

    class Cursor:
        def execute(self, statement):
            statements.append(statement)

    azure_sql.provision_delivery_user(Cursor(), "00112233-4455-6677-8899-aabbccddeeff")
    expected_sid = "0x33221100554477668899aabbccddeeff"
    assert "WHERE name='id-medw-delivery') <> " + expected_sid in statements[0]
    assert "CREATE USER [id-medw-delivery] WITH SID=" + expected_sid + ", TYPE=E" in statements[1]
    assert statements[2] == "ALTER ROLE db_owner ADD MEMBER [id-medw-delivery];"


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


def test_cost_reserves_os_qdrant_airflow_metadata_and_log_disks():
    quote = {kind: {"hourly_aud": 0.1} for kind in
             ("node", "registry", "sql", "disk", "load_balancer", "public_ip")}
    result = azure.estimate_cost(quote, 4)
    assert result["estimated_aud"] == 9.0
    assert result["is_hard_cap"] is False


@pytest.mark.parametrize(("key", "value"), [
    ("hour", 24), ("minute", 60), ("hour", True), ("max_documents", 0),
    ("max_documents", 1001), ("timezone", "Not/A_Timezone"),
])
def test_invalid_nightly_configuration_is_rejected(tmp_path, key, value):
    settings = config()
    settings["batch"][key] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(settings))
    with pytest.raises(azure.SetupError, match=f"batch.{key}"):
        azure.load_config(path)


def test_airflow_credentials_are_private_preserved_and_owned(tmp_path, monkeypatch):
    import base64

    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.config["owner"] = "our-test-deployment"
    existing, writes = {}, []
    monkeypatch.setattr(deployment, "kube", lambda *args: json.dumps(existing) if existing else "")
    monkeypatch.setattr(deployment, "apply", writes.append)
    deployment._airflow_secret()
    assert len(writes) == 1
    secret = writes[0]
    values = secret.pop("stringData")
    assert values["postgres-password"] in values["connection"]
    assert len(base64.urlsafe_b64decode(values["fernet-key"])) == 32
    assert len(set(values.values())) == len(values)
    existing.update(secret, data={k: base64.b64encode(v.encode()).decode() for k, v in values.items()})
    deployment._airflow_secret()
    assert len(writes) == 1
    assert "postgres-password" not in json.dumps(deployment.state)
    del existing["data"]["fernet-key"]
    with pytest.raises(azure.SetupError, match="incomplete"):
        deployment._airflow_secret()
    existing["metadata"]["labels"]["medw-owner"] = "someone-else"
    with pytest.raises(azure.SetupError, match="ownership"):
        deployment._airflow_secret()


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


@pytest.mark.parametrize("scenario", ["exists", "absent", "denied"])
def test_service_connection_cleanup_retries_preserve_other_connections(tmp_path, monkeypatch, scenario):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.state["service_connection"] = {"id": "owned", "name": "medw-azure"}
    endpoints = [{"id": "borrowed", "name": "medw-azure"}]
    if scenario == "exists":
        endpoints.append({"id": "owned", "name": "renamed-owned-connection"})
    deleted = []

    def listing(path):
        assert path == "serviceendpoint/endpoints?api-version=7.1"
        if scenario == "denied":
            raise azure.SetupError("access denied")
        return {"value": endpoints}

    def remove(*args, **kwargs):
        assert args[:3] == ("devops", "service-endpoint", "delete")
        identifier = args[args.index("--id") + 1]
        assert identifier == "owned"
        deleted.append(identifier)
        endpoints[:] = [item for item in endpoints if item["id"] != identifier]

    monkeypatch.setattr(deployment, "devops", listing)
    monkeypatch.setattr(azure, "az", remove)
    if scenario == "denied":
        with pytest.raises(azure.SetupError, match="access denied"):
            deployment._delete_service_connection()
    else:
        deployment._delete_service_connection()
        deployment._delete_service_connection()
    assert deleted == (["owned"] if scenario == "exists" else [])
    assert endpoints == [{"id": "borrowed", "name": "medw-azure"}]


def test_environment_generation_uses_azure_resources_and_bounded_scaling(tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.root = ROOT
    deployment.config["tenant_id"] = "tenant"
    deployment.state = {"resources": {"blob_url": "https://blobs.example", "cosmos_url": "https://cosmos.example",
        "search_url": "https://search.example", "sql_server": "sql.example", "registry_host": "acr.example"},
        "applications": {"api": {"appId": "client"}}, "hostname": "writer.example",
        "identities": {service: {"clientId": service + "-id", "principalId": service + "-principal"}
                       for service in (*azure.SERVICES, "airflow", "qdrant-backup")}}
    manifests = deployment.environment_values()
    application_cpu = 0
    for manifest in manifests:
        name, values = manifest["metadata"]["name"], manifest["spec"]["values"]
        if name == "qdrant":
            assert values["replicas"] == 1
            assert values["config"]["write_consistency_factor"] == 1
        elif name == "airflow":
            variables = {e["name"]: e["value"] for e in values["airflow"]["env"]}
            assert variables["MEDW_BATCH_SCHEDULE"] == "0 2 * * *"
            assert variables["MEDW_BATCH_TIMEZONE"] == "Australia/Brisbane"
            assert values["serviceAccount"]["annotations"]["azure.workload.identity/client-id"] == "airflow-id"
        else:
            assert values["autoscaling"]["maxReplicas"] == 2
            application_cpu += int(values["resources"]["requests"]["cpu"].removesuffix("m"))
            assert "demo_mode" not in values["config"]
            assert not values["config"].get("aoai_endpoint")
    assert application_cpu == 700  # leaves AKS/platform, second replica and rollout headroom


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


def test_recent_same_region_price_quote_is_reused(monkeypatch, tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)
    quote = {kind: {"hourly_aud": 0.1} for kind in
             ("node", "registry", "sql", "disk", "load_balancer", "public_ip")}
    azure.write_private(deployment.directory / "retail-prices.json", json.dumps({
        "location": deployment.config["location"], "currency": "AUD",
        "source": "https://prices.azure.com/api/retail/prices",
        "fetched_at": azure.dt.datetime.now(azure.dt.UTC).isoformat(), "quotes": quote}))
    monkeypatch.setattr(azure, "retail_prices", lambda _: pytest.fail("unnecessary retail API request"))
    assert deployment._cost()["estimated_aud"] == 9.0


def test_retail_rate_limit_retries_get_without_repeating_mutations(monkeypatch):
    import io
    attempts = []

    def request(*args, **kwargs):
        attempts.append(True)
        if len(attempts) < 2:
            raise azure.urllib.error.HTTPError("https://prices.example", 429, "slow down",
                                               {"Retry-After": "1"}, None)
        return io.BytesIO(b'{"Items": []}')

    monkeypatch.setattr(azure.urllib.request, "urlopen", request)
    monkeypatch.setattr(azure.time, "sleep", lambda _: None)
    assert azure.http_json("https://prices.example") == {"Items": []}
    assert len(attempts) == 2
    attempts.clear()
    with pytest.raises(azure.SetupError, match="HTTP 429"):
        azure.http_json("https://prices.example", method="POST", body={})
    assert len(attempts) == 1


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_lost_cosmos_create_response_is_cleaned_without_secret_evidence(monkeypatch, tmp_path, cleanup_fails):
    cfg = config()
    cfg["borrowed_cosmos_id"] = (
        "/subscriptions/sub/resourceGroups/shared/providers/Microsoft.DocumentDB/databaseAccounts/free")
    deployment = azure.Deployment(cfg, root=tmp_path)
    deployment.config["owner"] = "ours"
    deployment.state.update(owner="ours", config=copy.deepcopy(deployment.config))
    resource_id = cfg["borrowed_cosmos_id"] + "/sqlRoleAssignments/owned-assignment"

    def lost_response():
        assert json.loads(deployment.journal_path.read_text())["planned_cosmos_roles"][
            "cosmos-assignment-generation-platform-state"]["id"] == resource_id
        raise azure.SetupError("Bearer secret-token-was-echoed")

    with pytest.raises(azure.SetupError):
        deployment._cosmos_owned_operation("cosmos-assignment-generation-platform-state", resource_id, lost_response)
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[:5] == ("cosmosdb", "sql", "role", "assignment", "delete"):
            assert "--yes" in args
            if cleanup_fails:
                raise azure.SetupError("Bearer secret-token-was-echoed")

    monkeypatch.setattr(deployment, "account", lambda: None)
    monkeypatch.setattr(azure, "az", command)
    if cleanup_fails:
        with pytest.raises(azure.SetupError, match="Cleanup incomplete"):
            deployment.down()
    else:
        assert deployment.down()["complete"]
    assert any("owned-assignment" in call for call in calls)
    evidence = (deployment.directory / "cleanup.json").read_text()
    assert "secret-token" not in evidence
    if cleanup_fails:
        assert '"type": "SetupError"' in evidence


def test_arm_role_is_journalled_before_lost_create_response(monkeypatch, tmp_path):
    deployment = azure.Deployment(config(), root=tmp_path)

    def lost_response(*args, **kwargs):
        state = json.loads(deployment.journal_path.read_text())
        assert len(state["role_assignments"]) == 1
        raise azure.SetupError("lost response")

    monkeypatch.setattr(azure, "az", lost_response)
    with pytest.raises(azure.SetupError):
        deployment._role("principal", "Search Index Data Reader", "/subscriptions/sub/resourceGroups/shared")


@pytest.mark.parametrize("change", [None, "foreign_owner", "larger_node", "autoscaling"])
def test_quota_reuses_only_verified_owned_bounded_cluster(monkeypatch, tmp_path, change):
    deployment = azure.Deployment(config(), root=tmp_path)
    deployment.config.update(subscription_id="sub", owner="ours", prefix="test")
    resource = ("/subscriptions/sub/resourceGroups/rg-medw-dev/providers/"
                "Microsoft.ContainerService/managedClusters/testaks")
    deployment.state["completed"] = {"aks": {"id": resource}}
    pool = {"vmSize": "Standard_D4s_v5", "count": 1, "enableAutoScaling": False, "osDiskSizeGb": 64}
    if change == "larger_node":
        pool["vmSize"] = "Standard_D8s_v5"
    if change == "autoscaling":
        pool["enableAutoScaling"] = True

    def command(*args, **kwargs):
        if args[:2] == ("vm", "list-usage"):
            return [{"name": {"value": name}, "currentValue": 4, "limit": 4}
                    for name in ("cores", "standardDSv5Family")]
        if args[:2] == ("group", "show"):
            return {"tags": {"medw-owner": "other" if change == "foreign_owner" else "ours"}}
        if args[:2] == ("aks", "show"):
            return {"id": resource, "sku": {"tier": "Free"}, "agentPoolProfiles": [pool]}
        pytest.fail("Existing owned capacity must not require another node or SKU search")

    monkeypatch.setattr(azure, "az", command)
    if change:
        with pytest.raises(azure.SetupError):
            deployment._quota()
    else:
        assert "no additional vCPUs" in deployment._quota()

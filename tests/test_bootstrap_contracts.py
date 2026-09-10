"""Check generated request payloads and least-privilege role boundaries."""

import json
import pathlib
import shlex
import subprocess

from infra.search_payload import api_payload

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_search_request_excludes_documentation_but_keeps_generation_contract():
    request = api_payload(json.loads((ROOT / "infra/search/csr-chunks-index.json").read_text()))
    assert "_comment" not in request
    assert all(not any(k.startswith("_") for k in field) for field in request["fields"])
    names = {field["name"] for field in request["fields"]}
    assert {"index_generation", "evidence_chunk_id", "source_revision", "chunk_json"} <= names


def test_generation_cosmos_role_cannot_overwrite_evidence_or_active_pointer():
    role = json.loads((ROOT / "infra/cosmos/evidence-appender.json").read_text())
    actions = role["Permissions"][0]["DataActions"]
    assert any(a.endswith("/items/create") for a in actions)
    assert any(a.endswith("/items/read") for a in actions)
    assert not any(a.endswith(("replace", "upsert", "delete", "*")) for a in actions)
    assert role["AssignableScopes"] == ["/dbs/medw/colls/platform-state"]


def test_model_create_requests_pin_actual_version_and_upgrade_policy():
    for path in (ROOT / "infra/aoai").glob("*-deployment.json"):
        properties = json.loads(path.read_text())["properties"]
        assert properties["model"]["name"] and properties["model"]["version"]
        assert properties["versionUpgradeOption"] == "NoAutoUpgrade"


def test_bootstrap_shell_parses_without_running_azure():
    subprocess.run(["bash", "-n", str(ROOT / "infra/bootstrap.sh")], check=True)


def test_aks_bootstrap_uses_policy_enforcing_cilium_overlay():
    script = (ROOT / "infra/bootstrap.sh").read_text().replace("\\\n", " ")
    command = next(line for line in script.splitlines() if line.startswith("az aks create "))
    tokens = shlex.split(command)
    for flag, value in {
        "--network-plugin": "azure",
        "--network-plugin-mode": "overlay",
        "--network-dataplane": "cilium",
        "--pod-cidr": "$POD_CIDR",
        "--service-cidr": "$SERVICE_CIDR",
        "--dns-service-ip": "$DNS_SERVICE_IP",
    }.items():
        assert tokens[tokens.index(flag) + 1] == value


def test_sql_bootstrap_pins_proxy_for_tcp_1433_egress():
    script = (ROOT / "infra/bootstrap.sh").read_text().replace("\\\n", " ")
    command = next(
        line for line in script.splitlines() if line.startswith("az sql server conn-policy update ")
    )
    tokens = shlex.split(command)
    assert tokens[tokens.index("--connection-type") + 1] == "Proxy"
    assert tokens[tokens.index("-s") + 1] == "${PREFIX}sql"
    assert script.index("az sql server create ") < script.index(command) < script.index("az sql db create ")

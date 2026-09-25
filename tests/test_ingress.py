"""Render real Helm/Flux inputs to check routing, TLS and controller access together."""

import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(
    not shutil.which("helm") or not shutil.which("kubectl"), reason="Helm and kubectl required",
)


@pytest.fixture(scope="module")
def gateway_chart(tmp_path_factory):
    charts = tmp_path_factory.mktemp("ingress-charts")
    for name in ("gateway", "retrieval", "generation", "ingestion-worker", "medw-lib"):
        shutil.copytree(ROOT / "deploy/charts" / name, charts / name,
                        ignore=shutil.ignore_patterns("charts"))
    subprocess.run(["helm", "dependency", "build", str(charts / "gateway")],
                   check=True, capture_output=True)
    return charts / "gateway"


def render(chart, tmp_path, values):
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    output = subprocess.check_output([
        "helm", "template", "gateway", str(chart), "--namespace", "medw", "-f", str(path),
    ], text=True)
    return [doc for doc in yaml.safe_load_all(output) if doc]


@pytest.mark.parametrize("environment", ["base", "dev", "staging", "prod"])
def test_environment_route_and_network_policy_agree(gateway_chart, tmp_path, environment):
    output = subprocess.check_output([
        "kubectl", "kustomize", str(ROOT / "deploy/flux" / environment),
    ], text=True)
    release = next(doc for doc in yaml.safe_load_all(output)
                   if doc and doc["kind"] == "HelmRelease" and doc["metadata"]["name"] == "gateway")
    resources = render(gateway_chart, tmp_path, release["spec"].get("values", {}))
    ingress = next(doc for doc in resources if doc["kind"] == "VirtualServer")
    policy = next(doc for doc in resources if doc["kind"] == "NetworkPolicy")
    tls = True
    assert ingress["spec"]["ingressClassName"] == "medw-nginx"
    assert bool(ingress["spec"].get("tls")) == tls
    if tls:
        assert ingress["spec"]["tls"] == {
            "secret": "gateway-tls", "redirect": {"enable": True, "code": 308, "basedOn": "scheme"},
            "cert-manager": {"cluster-issuer": "letsencrypt", "issue-temp-cert": True},
        }
    for upstream in ingress["spec"]["upstreams"]:
        assert upstream["buffering"] is False
        assert upstream["read-timeout"] == "300s"
        assert upstream["next-upstream"] == "off"
    routes = ingress["spec"]["routes"]
    assert [route["action"]["pass"] for route in routes[:5]] == [
        "ingestion-worker", "ingestion-worker", "ingestion-worker", "retrieval", "generation",
    ]
    policies = {doc["metadata"]["name"]: doc["spec"]["externalAuth"]
                for doc in resources if doc["kind"] == "Policy"}
    for route in routes[:5]:
        auth = policies[route["policies"][0]["name"]]
        assert auth["authServiceName"] == "gateway" and auth["authServicePorts"] == [8000]
        assert "proxy_set_header X-Original-URI $request_uri;" in auth["authSnippets"]
        assert "proxy_set_header X-Original-Method $request_method;" in auth["authSnippets"]
        assert route["errorPages"][0]["return"]["code"] == 503
    assert len({auth["authURI"] for auth in policies.values()}) == 5
    assert routes[-1]["action"]["return"]["code"] == 404
    assert not any("/_internal" in route["path"] or "/metrics" in route["path"] for route in routes)
    assert any(rule["from"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "nginx-ingress"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "nginx-ingress"}},
    }] for rule in policy["spec"]["ingress"])


def test_controller_namespace_and_pod_selectors_are_configurable(gateway_chart, tmp_path):
    resources = render(gateway_chart, tmp_path, {"ingress": {"controller": {
        "namespace": "edge", "podLabels": {"app.kubernetes.io/name": "writer-edge"},
    }}})
    policy = next(doc for doc in resources if doc["kind"] == "NetworkPolicy")
    # Selectors in the same peer are ANDed; separate peers would allow either.
    assert policy["spec"]["ingress"][0]["from"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "edge"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "writer-edge"}},
    }]


def test_disabling_ingress_removes_its_controller_access(gateway_chart, tmp_path):
    resources = render(gateway_chart, tmp_path, {"ingress": {"enabled": False}})
    assert not any(doc["kind"] in {"Ingress", "VirtualServer", "Policy"} for doc in resources)
    policy = next(doc for doc in resources if doc["kind"] == "NetworkPolicy")
    # Prometheus may still scrape the gateway; the public controller has no rule.
    assert policy["spec"]["ingress"] == [{
        "from": [{"namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "monitoring"},
        }}],
        "ports": [{"protocol": "TCP", "port": 8000}],
    }]


@pytest.mark.parametrize("service", ["retrieval", "generation", "ingestion-worker"])
def test_backends_allow_controller_without_creating_public_routes(gateway_chart, tmp_path, service):
    chart = gateway_chart.parent / service
    subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
    resources = render(chart, tmp_path, {})
    assert not any(doc["kind"] in {"Ingress", "VirtualServer", "Policy"} for doc in resources)
    policy = next(doc for doc in resources if doc["kind"] == "NetworkPolicy")
    assert any(rule["from"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "nginx-ingress"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "nginx-ingress"}},
    }] for rule in policy["spec"]["ingress"])
    if service == "retrieval":
        assert any(rule["from"] == [{"podSelector": {"matchLabels": {"app": "generation"}}}]
                   for rule in policy["spec"]["ingress"])
    assert "gateway" not in str(policy["spec"]["ingress"])


def test_retired_local_routes_are_not_published(gateway_chart, tmp_path):
    resources = render(gateway_chart, tmp_path, {})
    server = next(doc for doc in resources if doc["kind"] == "VirtualServer")
    paths = [route["path"] for route in server["spec"]["routes"]]
    assert not any("/_synthetic/" in path or "/uploads/" in path for path in paths)

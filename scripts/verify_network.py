#!/usr/bin/env python3
"""Prove the chart's NetworkPolicies on the disposable medw-scaffold-proof context.

--install-calico adds pinned Calico enforcement to this disposable
minikube profile. It changes this profile's CNI only; no existing medw profile
is addressed. Run after verify_cluster.py, which loads the synthetic image.
"""

import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import yaml

CONTEXT = "medw-scaffold-proof"
NAMESPACE = "medw-network-proof"
ROOT = Path(__file__).resolve().parents[1]
CALICO = "https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml"
CALICO_SHA = "9a575859428b822a224dedafc4238555b6b0f910f2abf12983f20f871860914e"


def run(*args, data=None):
    return subprocess.check_output(list(args), input=data, text=True, stderr=subprocess.STDOUT)


def kube(*args, data=None):
    return run("kubectl", "--context", CONTEXT, *args, data=data)


def install_calico():
    content = urllib.request.urlopen(CALICO, timeout=30).read()
    assert hashlib.sha256(content).hexdigest() == CALICO_SHA
    manifest = content.decode().replace(
        '# - name: CALICO_IPV4POOL_CIDR\n            #   value: "192.168.0.0/16"',
        '- name: CALICO_IPV4POOL_CIDR\n              value: "10.244.0.0/16"')
    kube("apply", "--server-side", "-f", "-", data=manifest)
    kube("rollout", "status", "daemonset/calico-node", "-n", "kube-system", "--timeout=300s")
    # Keep the previous CNI config for inspection; the lexically first .conflist
    # is authoritative and otherwise minikube's bridge silently bypasses policy.
    changed = run("minikube", "-p", CONTEXT, "ssh", "--",
        "test -f /etc/cni/net.d/10-calico.conflist && "
        "if test -f /etc/cni/net.d/1-k8s.conflist; then "
        "sudo mv /etc/cni/net.d/1-k8s.conflist /etc/cni/net.d/1-k8s.conflist.proof-disabled; "
        "echo migrated; fi")
    if "migrated" in changed:
        # Existing bridge-backed pods need fresh CNI interfaces as well. This
        # profile is disposable; the original medw profile is never addressed.
        for namespace in ("flux-system", "keda", "monitoring"):
            kube("rollout", "restart", "deployment", "-n", namespace)
        kube("rollout", "restart", "deployment", "coredns", "calico-kube-controllers",
             "-n", "kube-system")
        for namespace in ("flux-system", "keda", "monitoring"):
            deployments = json.loads(kube("get", "deployments", "-n", namespace, "-o", "json"))
            for deployment in deployments["items"]:
                kube("rollout", "status", "deployment/" + deployment["metadata"]["name"],
                     "-n", namespace, "--timeout=300s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-calico", action="store_true")
    parser.add_argument("--output", default="/tmp/medw-network-proof-evidence.json")
    args = parser.parse_args()
    if args.install_calico:
        install_calico()
    kube("create", "namespace", NAMESPACE)
    try:
        for name, port in [("reranker", 8000), ("qdrant", 6333), ("retrieval", None),
                           ("outsider", None)]:
            command = ["python", "-m", "http.server", str(port)] if port else [
                "python", "-c", "import time; time.sleep(600)"]
            pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
                "name": name, "namespace": NAMESPACE, "labels": {"app": name}},
                "spec": {"containers": [{"name": "app", "image": "medw-generation:proof-a",
                                          "imagePullPolicy": "Never", "command": command,
                                          "resources": {"requests": {"memory": "64Mi"},
                                                        "limits": {"memory": "128Mi"}}}]}}
            kube("apply", "-f", "-", data=yaml.safe_dump(pod))
        kube("wait", "--for=condition=Ready", "pods", "--all", "-n", NAMESPACE, "--timeout=120s")
        ips = {name: kube("get", "pod", name, "-n", NAMESPACE,
                         "-o", "jsonpath={.status.podIP}").strip() for name in ("reranker", "qdrant")}
        def reachable(source, target, port):
            code = f"import urllib.request; urllib.request.urlopen('http://{ips[target]}:{port}',timeout=3).read()"
            result = subprocess.run(["kubectl", "--context", CONTEXT, "-n", NAMESPACE,
                                     "exec", source, "--", "python", "-c", code],
                                    capture_output=True, check=False)
            return result.returncode == 0
        # Establish connectivity first so later timeouts cannot be mistaken
        # for a policy success when the target was never running.
        assert reachable("outsider", "reranker", 8000)
        assert reachable("outsider", "qdrant", 6333)
        for chart in ("reranker", "retrieval", "qdrant"):
            rendered = run("helm", "template", chart, str(ROOT / "deploy/charts" / chart),
                           "--namespace", NAMESPACE)
            policies = [doc for doc in yaml.safe_load_all(rendered)
                        if doc and doc.get("kind") == "NetworkPolicy"]
            assert policies, f"No network policy rendered for {chart}"
            kube("apply", "-f", "-", "-n", NAMESPACE, data=yaml.safe_dump_all(policies))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not reachable("outsider", "reranker", 8000):
                break
            time.sleep(1)
        assert not reachable("outsider", "reranker", 8000)
        assert not reachable("outsider", "qdrant", 6333)
        assert reachable("retrieval", "reranker", 8000)
        assert reachable("retrieval", "qdrant", 6333)
        report = {"context": CONTEXT, "calico": "3.29.3", "result": "passed",
                  "checks": ["unrestricted baseline reaches both targets",
                             "unapproved caller blocked from reranker and Qdrant",
                             "retrieval caller reaches reranker and Qdrant"]}
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        kube("delete", "namespace", NAMESPACE, "--wait=true", "--timeout=120s")


if __name__ == "__main__":
    main()

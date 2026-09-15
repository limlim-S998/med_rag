#!/usr/bin/env python3
"""Exercise real NGINX, app images and Calico in a disposable local cluster.

Only the medw-nginx-proof context is accepted. Create it as described in README.
The script owns the medw/nginx-ingress namespaces there and removes them on exit.
No Azure credentials, external identity provider or clinical data are used.
"""

import argparse
import base64
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "medw-nginx-proof"
SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True, stderr=subprocess.STDOUT).strip()


def eventually(check, *, seconds=90):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except (AssertionError, httpx.HTTPError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", default="/tmp/medw-nginx-proof.kubeconfig")
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--output", default="/tmp/medw-ingress-evidence.json")
    args = parser.parse_args()
    kube_args = ("--kubeconfig", args.kubeconfig, "--context", CONTEXT)
    helm_args = ("--kubeconfig", args.kubeconfig, "--kube-context", CONTEXT)

    def kube(*params, data=None):
        return run("kubectl", *kube_args, *params, data=data)

    def apply(resource, namespace="medw"):
        return kube("apply", "-n", namespace, "-f", "-", data=yaml.safe_dump(resource))

    for namespace in ("medw", "nginx-ingress"):
        if kube("get", "namespace", namespace, "--ignore-not-found"):
            raise RuntimeError(f"{CONTEXT}/{namespace} already exists; use a fresh proof cluster")
    kube("get", "daemonset/calico-node", "-n", "kube-system")
    checks = []
    forwards = []
    with tempfile.TemporaryDirectory(prefix="medw-edge-proof-") as directory:
        work = Path(directory)
        try:
            for namespace in ("medw", "nginx-ingress"):
                kube("create", "namespace", namespace)
            release = next(doc for doc in yaml.safe_load_all(
                (ROOT / "deploy/nginx-ingress.yaml").read_text()) if doc["kind"] == "HelmRelease")
            values = release["spec"]["values"]
            # NodePort avoids depending on a cloud LoadBalancer in this local proof.
            values["controller"]["service"]["type"] = "NodePort"
            controller_values = work / "controller.yaml"
            controller_values.write_text(yaml.safe_dump(values))
            chart = release["spec"]["chart"]["spec"]
            print("Installing the pinned NGINX controller", flush=True)
            run("helm", "upgrade", "--install", "nginx-ingress", chart["chart"],
                "--repo", "https://helm.nginx.com/stable", "--version", chart["version"],
                "-n", "nginx-ingress", *helm_args, "-f", str(controller_values), "--wait", "--timeout", "5m")
            for name in (*SERVICES, "qdrant"):
                image = f"medw-{name}:{args.image_tag}" if name != "qdrant" else "qdrant/qdrant:v1.12.1"
                print(f"Loading {image}", flush=True)
                run("minikube", "-p", CONTEXT, "image", "load", image)
            apply({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                   "metadata": {"name": "medw-local-state"}, "spec": {
                       "accessModes": ["ReadWriteOnce"], "storageClassName": "standard",
                       "resources": {"requests": {"storage": "1Gi"}}}})
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            jwk.update(kid="proof", alg="RS256", use="sig")
            apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "identity"},
                   "data": {"keys": json.dumps({"keys": [jwk]})}})
            apply({"apiVersion": "v1", "kind": "Pod", "metadata": {
                "name": "identity", "labels": {"app": "identity"}}, "spec": {
                "containers": [{"name": "identity", "image": f"medw-gateway:{args.image_tag}",
                    "imagePullPolicy": "Never", "command": ["python", "-m", "http.server", "8000",
                    "--directory", "/keys"], "volumeMounts": [{"name": "keys", "mountPath": "/keys"}]}],
                "volumes": [{"name": "keys", "configMap": {"name": "identity"}}]}})
            apply({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "identity"},
                   "spec": {"selector": {"app": "identity"}, "ports": [{"port": 8000}]}})
            for name in ("qdrant", *SERVICES):
                values = {"image": {"tag": args.image_tag}, "replicas": 1,
                          "autoscaling": {"enabled": False}, "serviceMonitor": {"enabled": False},
                          "config": {"synthetic_enabled": True, "readiness_cache_seconds": 0}}
                if name == "qdrant":
                    values = {}
                if name == "gateway":
                    values["config"].update(auth_tenant_id="proof", auth_audience="medw-api",
                        auth_issuer="https://identity.test/proof", auth_jwks_url="http://identity:8000/keys")
                    values["networkPolicy"] = {"additionalEgress": [{"to": [
                        {"podSelector": {"matchLabels": {"app": "identity"}}}],
                        "ports": [{"protocol": "TCP", "port": 8000}]}]}
                path = work / f"{name}.yaml"
                path.write_text(yaml.safe_dump(values))
                chart_path = ROOT / "deploy/charts" / name
                run("helm", "dependency", "build", str(chart_path))
                run("helm", "upgrade", "--install", name, str(chart_path), *helm_args, "-n", "medw",
                    "-f", str(chart_path / "values-local.yaml"), "-f", str(path))
            for name in SERVICES:
                kube("rollout", "status", f"deployment/{name}", "-n", "medw", "--timeout=150s")
            kube("wait", "virtualserver/gateway", "-n", "medw",
                 "--for=jsonpath={.status.state}=Valid", "--timeout=90s")
            checks.append("pinned OSS controller accepts real VirtualServer/Policy resources")
            seed = '''
import asyncio, json
from medw_core.persistence import SQLiteStateStore
from medw_core.local.platform import LocalStudyAccess
from medw_core.durable_jobs import DurableJobStore
async def main():
    state = SQLiteStateStore('/data/platform.sqlite3')
    await LocalStudyAccess(state).grant('writer', 'allowed')
    job = await DurableJobStore(state).create('allowed', 'synthetic-document')
    print(json.dumps(job))
    await state.close()
asyncio.run(main())
'''
            job = json.loads(kube("exec", "-n", "medw", "deployment/gateway", "--", "python", "-c", seed))
            now = int(time.time())
            token = jwt.encode({"iss": "https://identity.test/proof", "aud": "medw-api",
                "tid": "proof", "oid": "writer", "iat": now, "nbf": now - 1, "exp": now + 600},
                key, algorithm="RS256", headers={"kid": "proof"})
            headers = {"Host": "medw.local", "Authorization": f"Bearer {token}"}

            def forward(remote):
                log = work / f"forward-{remote}.log"
                stream = log.open("w")
                process = subprocess.Popen(["kubectl", *kube_args, "-n", "nginx-ingress",
                    "port-forward", "svc/nginx-ingress-controller", f":{remote}"],
                    stdout=stream, stderr=subprocess.STDOUT)
                forwards.append((process, stream))

                def address():
                    match = re.search(r"127\.0\.0\.1:(\d+)", log.read_text())
                    assert match, log.read_text()
                    return f"http://127.0.0.1:{match[1]}"
                return eventually(address, seconds=20)

            base = forward(80)
            with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
                paths = [("GET", f"/studies/allowed/jobs/{job['id']}", 200),
                         ("POST", "/studies/allowed/search", 501),
                         ("POST", "/studies/allowed/sections/1.2/draft", 501)]
                for method, path, status in paths:
                    response = client.request(method, path, json={"study_id": "other"})
                    assert response.status_code == status, (path, response.status_code, response.text)
                    denied = client.request(method, path.replace("/allowed/", "/other/"), headers={
                        "X-Original-URI": path, "X-Original-Method": method})
                    assert denied.status_code == 403, (path, denied.status_code, denied.text)
                    missing = client.request(method, path, headers={"Authorization": ""})
                    assert missing.status_code == 401, (path, missing.status_code)
                assert client.get(paths[0][1]).json() == job
                assert client.get("/studies/allowed/jobs/missing").status_code == 404
                assert client.get("/studies/allowed/documents").status_code == 200
                checks.append("jobs route directly; search/draft stay 501; cross-study requests and spoofed headers denied")
                for path in ("/_internal/authorize/jobs", "/metrics", "/readyz", "/search", "/draft",
                             "/ingest", "/docs", "/jobs/allowed/missing"):
                    assert client.get(path).status_code == 404, path
                checks.append("internal APIs and operational surfaces are not publicly routed")
                start = time.monotonic()
                with client.stream("GET", "/_synthetic/work?seconds=3") as response:
                    lines = response.iter_lines()
                    assert json.loads(next(lines))["state"] == "started"
                    first = time.monotonic() - start
                    assert first < 2, first
                    assert json.loads(next(lines))["state"] == "finished"
                    assert time.monotonic() - start >= 3
                checks.append("real NGINX streams first synthetic chunk before completion")
                certificate, private_key = work / "tls.crt", work / "tls.key"
                run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=medw.local", "-addext", "subjectAltName=DNS:medw.local",
                    "-keyout", str(private_key), "-out", str(certificate))
                apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "gateway-tls"},
                    "type": "kubernetes.io/tls", "data": {
                        "tls.crt": base64.b64encode(certificate.read_bytes()).decode(),
                        "tls.key": base64.b64encode(private_key.read_bytes()).decode()}})
                server = json.loads(kube("get", "virtualserver/gateway", "-n", "medw", "-o", "json"))
                server["spec"]["tls"] = {"secret": "gateway-tls", "redirect": {
                    "enable": True, "code": 308, "basedOn": "scheme"}}
                apply(server)

                def redirect():
                    assert client.get("/version").status_code == 308
                eventually(redirect)
                tls_port = forward(443).rsplit(":", 1)[1]
                response = run("curl", "--silent", "--show-error", "--fail", "--max-time", "10",
                    "--noproxy", "*", "--resolve", f"medw.local:{tls_port}:127.0.0.1",
                    "--cacert", str(certificate), f"https://medw.local:{tls_port}/version")
                assert json.loads(response)["medical_handlers"] == "held-back"
                checks.append("TLS terminates with verified certificate/SNI and HTTP redirects with 308")
                del server["spec"]["tls"]
                # Merge-patch removes the field as well as the generated redirect.
                kube("patch", "virtualserver/gateway", "-n", "medw", "--type=merge",
                     "-p", '{"spec":{"tls":null}}')

                def plain_http():
                    assert client.get("/version").status_code == 200
                eventually(plain_http)
                target = kube("get", "service/ingestion-worker", "-n", "medw",
                              "-o", "jsonpath={.spec.clusterIP}")
                probe = "import socket; socket.create_connection((" + repr(target) + ",8000),2)"
                for namespace, labels in [("medw", {"app.kubernetes.io/name": "nginx-ingress"}),
                                          ("nginx-ingress", {"app": "outsider"})]:
                    apply({"apiVersion": "v1", "kind": "Pod", "metadata": {
                        "name": "outsider", "labels": labels}, "spec": {"containers": [{
                            "name": "probe", "image": f"medw-gateway:{args.image_tag}",
                            "imagePullPolicy": "Never", "command": ["python", "-c",
                            "import time; time.sleep(600)"]}]}}, namespace)
                    kube("wait", "pod/outsider", "-n", namespace,
                         "--for=condition=Ready", "--timeout=60s")
                    result = subprocess.run(["kubectl", *kube_args, "exec", "-n", namespace,
                        "outsider", "--", "python", "-c", probe],
                        capture_output=True, text=True, check=False)
                    assert result.returncode != 0 and "TimeoutError" in result.stderr, result.stderr
                checks.append("Calico rejects matching labels in wrong namespace and wrong labels in right namespace")
                kube("scale", "deployment/retrieval", "-n", "medw", "--replicas=0")

                def backend_outage():
                    assert client.post("/studies/allowed/search").status_code == 503
                    assert client.get("/studies/allowed/documents").status_code == 200
                eventually(backend_outage)
                checks.append("backend outage returns 503 without disabling writer/auth operations")
                kube("scale", "deployment/gateway", "-n", "medw", "--replicas=0")

                def auth_outage():
                    assert client.get(paths[0][1]).status_code == 503
                eventually(auth_outage)
                checks.append("auth outage fails closed before a healthy jobs backend")
            print(json.dumps({"result": "passed", "checks": checks}, indent=2), flush=True)
            Path(args.output).write_text(json.dumps({"result": "passed", "context": CONTEXT,
                "image_tag": args.image_tag, "controller": "5.6.1", "chart": "2.7.1",
                "checks": checks}, indent=2) + "\n")
        except subprocess.CalledProcessError as exc:
            print(exc.output, flush=True)
            raise
        finally:
            for process, stream in forwards:
                process.terminate()
                process.wait(timeout=10)
                stream.close()
            for namespace in ("medw", "nginx-ingress"):
                kube("delete", "namespace", namespace, "--ignore-not-found", "--wait=false")


if __name__ == "__main__":
    main()

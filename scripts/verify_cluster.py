#!/usr/bin/env python3
"""Disposable Flux image/chart/rollback and Prometheus/KEDA concurrency proof.

Uses only context medw-scaffold-proof. The source is a temporary local Git
repository served over smart HTTP on minikube's host bridge. It never pushes
or changes the real repository. Requires Docker, minikube, kubectl, Helm, Flux
and the project Python environment. Evidence is written to --output.
"""

import argparse
import asyncio
import contextlib
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "medw-scaffold-proof"


def run(*args, cwd=None, data=None):
    return subprocess.check_output(list(args), cwd=cwd, input=data, text=True,
                                   stderr=subprocess.STDOUT).strip()


def kube(*args, data=None):
    return run("kubectl", "--context", CONTEXT, *args, data=data)


def apply(obj):
    kube("apply", "-f", "-", data=yaml.safe_dump(obj))


def wait_for(fn, seconds=180):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (httpx.HTTPError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
            last = str(exc)
        time.sleep(2)
    raise AssertionError(f"Verification timed out: {last}")


class GitHTTP(http.server.BaseHTTPRequestHandler):
    """Small read-only smart HTTP bridge around the actual git-http-backend."""

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def log_message(self, format, *args):
        pass

    def respond(self):
        parsed = urlsplit(self.path)
        if "git-receive-pack" in self.path:
            self.send_error(403)
            return
        env = {**os.environ, "GIT_PROJECT_ROOT": self.server.repo_root,
               "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
               "PATH_INFO": parsed.path, "QUERY_STRING": parsed.query,
               "CONTENT_TYPE": self.headers.get("content-type", ""),
               "REMOTE_ADDR": self.client_address[0]}
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        result = subprocess.check_output(["git", "http-backend"], env=env, input=body)
        header, payload = result.split(b"\r\n\r\n", 1)
        headers = dict(line.decode().split(": ", 1) for line in header.split(b"\r\n"))
        self.send_response(int(headers.pop("Status", "200 OK").split()[0]))
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextlib.contextmanager
def forward(namespace, resource, remote):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", namespace,
                             "port-forward", resource, f"{port}:{remote}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def connected():
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if connected():
                    break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError("Port forward did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def setup():
    run("minikube", "start", "-p", CONTEXT, "--driver=docker", "--memory=6144", "--cpus=4",
        "--kubernetes-version=v1.32.2")
    run("flux", "install", "--context=" + CONTEXT,
        "--components=source-controller,helm-controller,kustomize-controller", "--timeout=5m")
    run("helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts")
    run("helm", "repo", "add", "prometheus-community", "https://prometheus-community.github.io/helm-charts")
    run("helm", "upgrade", "--install", "keda", "kedacore/keda", "--version", "2.17.2",
        "--kube-context", CONTEXT, "--namespace", "keda", "--create-namespace", "--wait")
    values = {"alertmanager": {"enabled": False}, "prometheus-pushgateway": {"enabled": False},
              "prometheus-node-exporter": {"enabled": False}, "kube-state-metrics": {"enabled": True},
              "server": {"persistentVolume": {"enabled": False},
                         "global": {"scrape_interval": "5s", "scrape_timeout": "4s"},
                         "resources": {"requests": {"memory": "256Mi"},
                                       "limits": {"memory": "768Mi"}}},
              "extraScrapeConfigs": yaml.safe_dump([{
                  "job_name": "medw-synthetic", "kubernetes_sd_configs": [
                      {"role": "pod", "namespaces": {"names": ["medw"]}}],
                  "relabel_configs": [
                      {"source_labels": ["__meta_kubernetes_pod_label_app"],
                       "regex": "generation", "action": "keep"},
                      {"source_labels": ["__meta_kubernetes_pod_container_port_number"],
                       "regex": "8000", "action": "keep"},
                      {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"},
                  ]}])}
    run("helm", "upgrade", "--install", "prometheus", "prometheus-community/prometheus",
        "--version", "27.11.0", "--kube-context", CONTEXT, "--namespace", "monitoring",
        "--create-namespace", "--values", "-", "--wait", "--timeout", "300s",
        data=yaml.safe_dump(values))


def current_deployment():
    return json.loads(kube("get", "deployment", "generation", "-n", "medw", "-o", "json"))


def ready_image(tag):
    value = current_deployment()
    return value["spec"]["template"]["spec"]["containers"][0]["image"] == tag and (
        value.get("status", {}).get("observedGeneration") == value["metadata"]["generation"] and
        value.get("status", {}).get("updatedReplicas", 0) == value["spec"]["replicas"] and
        value.get("status", {}).get("availableReplicas", 0) == value["spec"]["replicas"] and
        value.get("status", {}).get("unavailableReplicas", 0) == 0)


async def workload(url):
    async with httpx.AsyncClient(timeout=40) as client:
        async def worker():
            for _ in range(3):
                response = await client.get(url + "/_synthetic/work?seconds=30")
                response.raise_for_status()
                assert '"state": "finished"' in response.text
        await asyncio.gather(*(worker() for _ in range(12)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--output", default="/tmp/medw-scaffold-proof-evidence.json")
    args = parser.parse_args()
    if not args.skip_setup:
        print("Installing disposable cluster controllers", flush=True)
        setup()
    print("Building and loading two synthetic image artifacts", flush=True)
    for label in ("a", "b"):
        run("docker", "build", "-f", "services/generation/Dockerfile", "--label",
            f"medw.synthetic.release={label}", "-t", f"medw-generation:proof-{label}", ".", cwd=ROOT)
        run("minikube", "-p", CONTEXT, "image", "load", f"medw-generation:proof-{label}")
    image_ids = [run("docker", "image", "inspect", f"medw-generation:proof-{label}",
                     "--format", "{{.Id}}") for label in ("a", "b")]
    assert image_ids[0] != image_ids[1]
    report = {"image_config_digests": image_ids, "context": CONTEXT, "kubernetes": "1.32.2", "keda": "2.17.2",
              "prometheus_chart": "27.11.0", "checks": []}
    with tempfile.TemporaryDirectory(prefix="medw-flux-proof-") as temp:
        source = Path(temp) / "source"
        source.mkdir()
        shutil.copytree(ROOT / "deploy/charts", source / "deploy/charts")
        release = yaml.safe_load((ROOT / "deploy/flux/base/generation.yaml").read_text())
        release["spec"]["interval"] = "5s"
        release["spec"]["chart"]["spec"]["interval"] = "5s"
        values = {"env": "local", "image": {"repository": "medw-generation", "tag": "proof-a",
                  "digest": "", "sourceSha": "a" * 40, "pullPolicy": "Never"},
                  "config": {"backend": "local", "synthetic_enabled": True},
                  "secretEnv": [], "serviceAccount": {"annotations": {}},
                  "serviceMonitor": {"enabled": False}, "networkPolicy": {"enabled": False},
                  "autoscaling": {"enabled": True, "metric": "inflight_requests", "target": 2,
                    "minReplicas": 1, "maxReplicas": 4, "pollingInterval": 5,
                    "cooldownPeriod": 10, "stabilizationWindowSeconds": 15,
                    "prometheusAddress": "http://prometheus-server.monitoring"},
                  "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"}}}
        release["spec"]["values"] = values
        manifest_dir = source / "proof"
        manifest_dir.mkdir()
        def write_release():
            (manifest_dir / "release.yaml").write_text(yaml.safe_dump(release))
        write_release()
        (manifest_dir / "namespace.yaml").write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: medw\n")
        (manifest_dir / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources: [namespace.yaml, release.yaml]\n")
        run("git", "init", "-b", "main", cwd=source)
        run("git", "config", "user.email", "synthetic@example.invalid", cwd=source)
        run("git", "config", "user.name", "Synthetic platform proof", cwd=source)
        run("git", "config", "commit.gpgsign", "false", cwd=source)
        def commit(message, push=True):
            run("git", "add", ".", cwd=source)
            run("git", "commit", "-m", message, cwd=source)
            if push:
                run("git", "push", str(Path(temp) / "repo.git"), "main", cwd=source)
            return run("git", "rev-parse", "HEAD", cwd=source)
        commit("Synthetic baseline", push=False)
        run("git", "clone", "--bare", str(source), str(Path(temp) / "repo.git"))
        bridge = run("minikube", "-p", CONTEXT, "ssh", "--", "getent hosts host.minikube.internal").split()[0]
        server = http.server.ThreadingHTTPServer((bridge, 0), GitHTTP)
        server.repo_root = temp
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print("Verifying Flux baseline, image change, chart change and rollback", flush=True)
            apply({"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                   "metadata": {"name": "medwriter-assist", "namespace": "flux-system"},
                   "spec": {"interval": "5s", "url": f"http://host.minikube.internal:{server.server_port}/repo.git",
                            "ref": {"branch": "main"}}})
            apply({"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                   "metadata": {"name": "medw-proof", "namespace": "flux-system"},
                   "spec": {"interval": "5s", "path": "./proof", "prune": True,
                            "sourceRef": {"kind": "GitRepository", "name": "medwriter-assist"}}})
            wait_for(functools.partial(ready_image, "medw-generation:proof-a"), 300)
            report["checks"].append("Flux installed baseline generation image")
            values["image"].update(tag="proof-b", sourceSha="b" * 40)
            write_release()
            report["image_commit"] = commit("Image-only release change")
            wait_for(functools.partial(ready_image, "medw-generation:proof-b"))
            report["checks"].append("Flux applied image-only release change")
            template = source / "deploy/charts/medw-lib/templates/_deployment.yaml"
            # Change rendered pod metadata without a chart version increment.
            template.write_text(template.read_text().replace(
                "      labels:\n", "      annotations: { medw-proof: chart-only }\n      labels:\n", 1))
            run("helm", "dependency", "update", str(source / "deploy/charts/generation"))
            report["chart_commit"] = commit("Chart-only change with unchanged chart version")
            wait_for(lambda: current_deployment()["spec"]["template"]["metadata"].get(
                "annotations", {}).get("medw-proof") == "chart-only")
            wait_for(functools.partial(ready_image, "medw-generation:proof-b"))
            report["checks"].append("Flux applied chart-only change at unchanged chart version/image")
            values["image"].update(tag="proof-a", sourceSha="a" * 40)
            write_release()
            report["rollback_commit"] = commit("Rollback to baseline artifact")
            wait_for(functools.partial(ready_image, "medw-generation:proof-a"))
            report["checks"].append("Flux rolled back to baseline image")
            print("Verifying Prometheus and KEDA under bounded synthetic load", flush=True)
            with forward("medw", "service/generation", 8000) as app_url, forward(
                "monitoring", "service/prometheus-server", 80
            ) as prometheus_url, httpx.Client(timeout=10) as client:
                assert client.get(app_url + "/readyz").status_code == 200
                client.get(app_url + "/_synthetic/work?seconds=0").raise_for_status()
                def gauge():
                    result = client.get(prometheus_url + "/api/v1/query", params={
                        "query": 'sum(medw_inflight_requests{app="generation"})'
                    }).raise_for_status().json()["data"]["result"]
                    return float(result[0]["value"][1]) if result else None
                wait_for(lambda: gauge() == 0)
                failures = []
                def load():
                    try:
                        asyncio.run(workload(app_url))
                    except (httpx.HTTPError, AssertionError) as exc:
                        failures.append(str(exc))
                load_thread = threading.Thread(target=load)
                load_thread.start()
                wait_for(lambda: (gauge() or 0) >= 10)
                report["peak_inflight"] = gauge()
                wait_for(lambda: current_deployment()["status"].get("availableReplicas", 0) >= 2)
                report["scaled_up_replicas"] = current_deployment()["status"]["availableReplicas"]
                load_thread.join(timeout=130)
                assert not load_thread.is_alive() and not failures, failures
                wait_for(lambda: gauge() == 0)
                wait_for(lambda: current_deployment()["spec"]["replicas"] == 1, 180)
                report["scaled_down_replicas"] = 1
                report["final_inflight"] = gauge()
                report["checks"].append("Prometheus measured bounded synthetic load; KEDA scaled up and down")
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
        finally:
            kube("delete", "kustomization", "medw-proof", "-n", "flux-system", "--ignore-not-found")
            kube("delete", "gitrepository", "medwriter-assist", "-n", "flux-system", "--ignore-not-found")
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()

"""Render the pinned controller and check its contract with the gateway; no cluster writes."""

import pathlib
import subprocess
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> None:
    documents = list(yaml.safe_load_all((ROOT / "deploy/flux/platform/controllers/nginx.yaml").read_text()))
    release = next(doc for doc in documents if doc["kind"] == "HelmRelease")
    repository = next(doc for doc in documents if doc["kind"] == "HelmRepository")
    spec = release["spec"]
    chart = spec["chart"]["spec"]
    with tempfile.TemporaryDirectory(prefix="medw-ingress-") as directory:
        values = pathlib.Path(directory) / "values.yaml"
        values.write_text(yaml.safe_dump(spec["values"]))
        output = subprocess.check_output([
            "helm", "template", spec["releaseName"], chart["chart"],
            "--repo", repository["spec"]["url"], "--version", chart["version"],
            "--namespace", release["metadata"]["namespace"], "--include-crds", "-f", str(values),
        ], text=True)
    resources = list(yaml.safe_load_all(output))
    deployment = next(doc for doc in resources if doc and doc["kind"] == "Deployment")
    ingress_class = next(doc for doc in resources if doc and doc["kind"] == "IngressClass")
    gateway = yaml.safe_load((ROOT / "deploy/charts/gateway/values.yaml").read_text())["ingress"]
    controller = gateway["controller"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if deployment["metadata"]["namespace"] != controller["namespace"] or any(
        labels.get(key) != value for key, value in controller["podLabels"].items()
    ):
        raise ValueError("gateway NetworkPolicy would exclude the rendered controller")
    if ingress_class["metadata"]["name"] != gateway["className"]:
        raise ValueError("gateway and controller IngressClass differ")
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    flags = dict(arg.split("=", 1) for arg in args if "=" in arg)
    watched = flags.get("-watch-namespace", "").split(",")
    namespace = deployment["metadata"]["namespace"]
    if namespace not in watched or "medw" not in watched:
        raise ValueError("controller must watch gateway and external Service namespaces")
    external = flags["-external-service"]
    if not any(doc and doc["kind"] == "Service" and doc["metadata"]["name"] == external
               and doc["metadata"]["namespace"] == namespace for doc in resources):
        raise ValueError("controller external Service does not exist")
    if flags.get("-nginx-plus") != "false":
        raise ValueError("scaffold requires NGINX Open Source without a Plus license")
    if flags.get("-enable-custom-resources") != "true" or flags.get("-enable-snippets") != "true":
        raise ValueError("external auth requires custom resources and checked-in header snippets")
    crds = {doc["metadata"]["name"] for doc in resources
            if doc and doc["kind"] == "CustomResourceDefinition"}
    if not {"virtualservers.k8s.nginx.org", "policies.k8s.nginx.org"} <= crds:
        raise ValueError("controller chart does not install the required CRDs")
    for service in ("retrieval", "generation", "ingestion-worker"):
        ingress = yaml.safe_load((ROOT / f"deploy/charts/{service}/values.yaml").read_text())["ingress"]
        if not ingress["enabled"] or ingress["controller"] != controller:
            raise ValueError(f"{service} must permit the same NGINX controller as gateway")
    if gateway["readTimeout"] != "300s":
        raise ValueError("data upstreams must use the configured stream timeout")
    print(f"  ok   F5 NGINX {chart['version']} controller/auth/routing contract")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build/load images and publish a source snapshot to a disposable local Git repo.

Example (serve SOURCE_DIR through an in-cluster HTTP server):
  local_deploy.sh --context medw-scaffold-proof --source-dir /tmp/medw-proof.git \
      --source-url http://git-source.flux-system.svc.cluster.local/repo.git

Flux controllers must already be installed on that explicit context. This
script never fetches or pushes the project's origin and never uses helm upgrade.
"""
from __future__ import annotations

import argparse
import ipaddress
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

import yaml

if __package__:
    from .release import ROOT, SERVICES
else:
    from release import ROOT, SERVICES


def run(*args: str, cwd: pathlib.Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def local_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "http":
        return False
    if host == "localhost" or host.endswith((".svc", ".svc.cluster.local")):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--source-dir", required=True, type=pathlib.Path)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--tag")
    args = parser.parse_args()
    if not args.context.endswith(("-proof", "-local")) or not local_url(args.source_url):
        parser.error("requires a disposable -proof/-local context and a private HTTP Git source")
    source = args.source_dir.resolve()
    if not source.is_relative_to(pathlib.Path(tempfile.gettempdir())):
        parser.error("disposable bare source directory must be under the temporary directory")
    tag = args.tag or f"local-{time.time_ns()}"
    if not args.skip_build:
        run(sys.executable, str(ROOT / "scripts/build_images.py"), "--tag", tag)
    for service in SERVICES:
        run("minikube", "image", "load", f"medw-{service}:{tag}", "-p", args.context)
    with tempfile.TemporaryDirectory(prefix="medw-source-") as directory:
        checkout = pathlib.Path(directory) / "source"
        if source.exists():
            run("git", "clone", str(source), str(checkout))
        else:
            source.mkdir(parents=True)
            run("git", "init", "--bare", "--initial-branch=main", str(source))
            run("git", "clone", str(source), str(checkout))
        shutil.copytree(ROOT, checkout, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            ".git", ".venv", ".env", ".env.*", "holding", "data", "__pycache__", "*.pyc",
            "*.egg-info", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".codex", ".agents"))
        path = checkout / "deploy/flux/base/source.yaml"
        git_source = yaml.safe_load(path.read_text())
        git_source["spec"]["url"] = args.source_url
        path.write_text(yaml.safe_dump(git_source))
        path = checkout / "deploy/flux/local/kustomization.yaml"
        overlay = yaml.safe_load(path.read_text())
        for patch in overlay["patches"]:
            if patch["target"]["name"] not in SERVICES:
                continue
            values = yaml.safe_load(patch["patch"])[0]["value"]
            values["image"].update(tag=tag, digest="", sourceSha="unversioned")
            values.setdefault("config", {}).update(backend="local")
            values["env"] = "local"
            values["networkPolicy"] = {"enabled": False}
            patch["patch"] = yaml.safe_dump([{"op": "add", "path": "/spec/values", "value": values}])
        path.write_text(yaml.safe_dump(overlay, sort_keys=False))
        run("git", "add", ".", cwd=checkout)
        run("git", "-c", "user.name=Local Scaffold", "-c", "user.email=local@medwriter.invalid",
            "commit", "-m", f"local scaffold {tag}", cwd=checkout)
        run("git", "push", "origin", "HEAD:main", cwd=checkout)
        run("git", "update-server-info", cwd=source)
        bootstrap = [git_source, {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
            "metadata": {"name": "medw-local", "namespace": "flux-system"},
            "spec": {"interval": "10s", "path": "./deploy/flux/local", "prune": True,
                     "sourceRef": {"kind": "GitRepository", "name": "medwriter-assist"}},
        }]
        subprocess.run(["kubectl", "--context", args.context, "apply", "-f", "-"],
                       input=yaml.safe_dump_all(bootstrap), text=True, check=True)
    print(f"Published {tag} to disposable source {source}; Flux owns reconciliation.")


if __name__ == "__main__":
    main()

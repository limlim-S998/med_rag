#!/usr/bin/env python3
"""Read-only verification of published/copied artifacts before promotion.

The destination registry must already contain each exact digest. No image is
rebuilt or copied by this command. Docker's pull verifies content digests.
"""
import argparse
import json
import pathlib
import subprocess

import yaml

if __package__:
    from .release import ROOT, validate
else:
    from release import ROOT, validate


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--environment", choices=("dev", "staging", "prod"))
    args = parser.parse_args()
    bundle = validate(json.loads(args.bundle.read_text()))
    run("git", "cat-file", "-e", f"{bundle['chart_source_sha']}:deploy/charts")
    repositories = {}
    if args.environment:
        path = ROOT / "deploy/flux" / args.environment / "environment-values.yaml"
        repositories = {doc["metadata"]["name"]: doc["spec"].get("values", {}).get("image", {}).get("repository")
                        for doc in yaml.safe_load_all(path.read_text())}
    for service, artifact in bundle["images"].items():
        repository = f"{args.registry}/{service}"
        if args.environment and repositories.get(service) != repository:
            raise ValueError(f"{service}: verified registry differs from selected environment repository")
        image = f"{repository}@{artifact['digest']}"
        run("docker", "pull", image)
        revision = run("docker", "image", "inspect", image, "--format",
                       '{{index .Config.Labels "org.opencontainers.image.revision"}}')
        if revision != artifact["source_sha"]:
            raise ValueError(f"{service}: built source revision differs from release metadata")
        if service == "generation":
            actual = run("docker", "run", "--rm", "--network", "none", image, "python", "-c",
                         "from pathlib import Path; from medw_core.content import prompt_hash; "
                         "print(prompt_hash(Path('/app/app/prompts')))")
            if actual != bundle["behavior"]["prompt_bundle_sha"]:
                raise ValueError("packaged prompt content differs from release metadata")
    print(f"verified all five artifacts at {args.registry}: {bundle['bundle_sha']}")


if __name__ == "__main__":
    main()

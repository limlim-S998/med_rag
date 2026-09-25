#!/usr/bin/env python3
"""Smoke and publish Bake's existing images; emit a complete release manifest."""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

if __package__:
    from .release import ARTIFACTS, ROOT
else:
    from release import ARTIFACTS, ROOT


def run(*args: str, capture: bool = False) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          capture_output=capture).stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    if run("git", "status", "--porcelain", capture=True).strip():
        parser.error("Publishing requires a clean source checkout")
    source = run("git", "rev-parse", "HEAD", capture=True).strip()
    for service in ARTIFACTS:
        image = f"{args.registry}/{service}:{source}"
        revision = run("docker", "image", "inspect", image, "--format",
                       '{{index .Config.Labels "org.opencontainers.image.revision"}}', capture=True).strip()
        if revision != source:
            raise ValueError(f"{service}: built image belongs to a different source revision")
        run(sys.executable, "scripts/smoke_image.py", "--image", image, "--service", service)
    artifacts = {}
    for service in ARTIFACTS:
        repository = f"{args.registry}/{service}"
        image = f"{repository}:{source}"
        run("docker", "push", image)
        refs = json.loads(run("docker", "image", "inspect", image, "--format",
                              "{{json .RepoDigests}}", capture=True))
        digests = [ref.split("@", 1)[1] for ref in refs if ref.startswith(repository + "@")]
        if len(digests) != 1:
            raise ValueError(f"{service}: registry digest could not be established")
        artifacts[service] = {"digest": digests[0], "source_sha": source}
    args.output.write_text(json.dumps(artifacts, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

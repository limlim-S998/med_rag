#!/usr/bin/env python3
"""Build all service artifacts; optionally push and record registry digests.

Local dirty builds use an explicit local tag and are not release artifacts.
Publishing requires a clean checkout and records the exact source revision.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

if __package__:
    from .release import ROOT, SERVICES
else:
    from release import ROOT, SERVICES


def run(*args: str, capture: bool = False) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          capture_output=capture).stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="")
    parser.add_argument("--tag", default="scaffold-local")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("/tmp/medw-images.json"))
    parser.add_argument("--service", choices=SERVICES, action="append")
    args = parser.parse_args()
    dirty = run("git", "status", "--porcelain", capture=True).strip()
    source = run("git", "rev-parse", "HEAD", capture=True).strip()
    if args.push and (dirty or not args.registry):
        parser.error("publishing requires a clean checkout and --registry")
    source = source if not dirty else "unversioned"
    tag = source if args.push else args.tag

    artifacts = {}
    for service in args.service or SERVICES:
        directory = service.replace("-", "_")
        repository = f"{args.registry}/{service}" if args.registry else f"medw-{service}"
        image = f"{repository}:{tag}"
        run("docker", "build", "--build-arg", f"SOURCE_SHA={source}", "-f",
            f"services/{directory}/Dockerfile", "-t", image, ".")
        run(sys.executable, "scripts/smoke_image.py", "--image", image, "--service", service)
        if args.push:
            run("docker", "push", image)
            refs = json.loads(run("docker", "image", "inspect", image, "--format",
                                  "{{json .RepoDigests}}", capture=True))
            matching = [ref.split("@", 1)[1] for ref in refs if ref.startswith(repository + "@")]
            if len(matching) != 1:
                raise RuntimeError(f"could not verify registry digest for {service}")
            artifacts[service] = {"digest": matching[0], "source_sha": source}
    args.output.write_text(json.dumps(artifacts, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

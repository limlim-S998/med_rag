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
import time
from concurrent.futures import ThreadPoolExecutor

if __package__:
    from .release import ARTIFACTS, ROOT
else:
    from release import ARTIFACTS, ROOT


def run(*args: str, capture: bool = False) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          capture_output=capture).stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="")
    parser.add_argument("--tag", default="scaffold-local")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("/tmp/medw-images.json"))
    parser.add_argument("--service", choices=ARTIFACTS, action="append")
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=2,
                        help="Maximum simultaneous build/smoke/push operations")
    args = parser.parse_args()
    dirty = run("git", "status", "--porcelain", capture=True).strip()
    source = run("git", "rev-parse", "HEAD", capture=True).strip()
    if args.push and (dirty or not args.registry):
        parser.error("publishing requires a clean checkout and --registry")
    source = source if not dirty else "unversioned"
    tag = source if args.push else args.tag

    # A failed rerun must not leave a previous successful manifest at this path.
    args.output.unlink(missing_ok=True)

    def build(service):
        started = time.monotonic()
        print(f"[start] image {service}", flush=True)
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
            artifact = {"digest": matching[0], "source_sha": source}
        else:
            artifact = None
        print(f"[succeeded] image {service} ({time.monotonic() - started:.1f}s)", flush=True)
        return service, artifact

    # Start the larger Airflow build early. Each image is smoked before its push;
    # the complete release manifest appears only after every image succeeds.
    services = sorted(set(args.service or ARTIFACTS), key=lambda name: (name != "airflow", name))
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        artifacts = {service: artifact for service, artifact in executor.map(build, services) if artifact is not None}
    args.output.write_text(json.dumps(artifacts, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

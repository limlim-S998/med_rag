#!/usr/bin/env python3
"""Commit a validated release in a temporary checkout and push with retry.

Intended for an authenticated CI checkout. Never pushes without --push.
Only the selected environment pointer and immutable release manifest are added.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile

import yaml

if __package__:
    from .release import ROOT, SHA, select, validate
else:
    from release import ROOT, SHA, select, validate


def git(*args: str, cwd: pathlib.Path = ROOT, check: bool = True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)



def assert_forward(bundle: dict, environment: str, work: pathlib.Path) -> None:
    pointer = work / "deploy/flux" / environment / "release-values.yaml"
    if not pointer.exists():
        return
    for release in yaml.safe_load_all(pointer.read_text()):
        name = release["metadata"]["name"]
        if release.get("kind") == "GitRepository":
            previous = release["spec"].get("ref", {}).get("commit", "")
            current = bundle["chart_source_sha"]
            if SHA.fullmatch(previous) and git(
                "merge-base", "--is-ancestor", previous, current, cwd=work, check=False
            ).returncode:
                raise ValueError("refusing a stale chart revision; use explicit rollback")
            continue
        if name not in bundle["images"]:
            continue
        previous = release["spec"].get("values", {}).get("image", {}).get("sourceSha", "")
        if not SHA.fullmatch(previous):
            continue
        current = bundle["images"][name]["source_sha"]
        if git("merge-base", "--is-ancestor", previous, current, cwd=work, check=False).returncode:
            raise ValueError(f"{name}: refusing a stale/non-descendant release; use explicit rollback")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    parser.add_argument("--environment", choices=("dev", "staging", "prod"), required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--allow-rollback", action="store_true")
    args = parser.parse_args()
    bundle = validate(json.loads(args.bundle.read_text()))
    if not args.push:
        print(f"would select {bundle['bundle_sha']} for {args.environment} on {args.branch}")
        return
    # Temporary worktree uses the original checkout's credentials; no tracked
    # changes in the caller are reset, committed or included in the release.
    for _attempt in range(4):
        git("fetch", args.remote, args.branch)
        with tempfile.TemporaryDirectory(prefix="medw-release-") as temp:
            work = pathlib.Path(temp) / "checkout"
            git("worktree", "add", "--detach", str(work), "FETCH_HEAD")
            try:
                if not args.allow_rollback:
                    assert_forward(bundle, args.environment, work)
                target = select(bundle, args.environment, root=work)
                manifest = work / "deploy/releases" / (bundle["bundle_sha"].split(":")[1] + ".json")
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(json.dumps(bundle, sort_keys=True, indent=2) + "\n")
                git("add", str(target.relative_to(work)), str(manifest.relative_to(work)), cwd=work)
                if git("diff", "--cached", "--quiet", cwd=work, check=False).returncode == 0:
                    print("release already selected")
                    return
                git("-c", "user.name=Medwriter Release", "-c", "user.email=release@medwriter.invalid",
                    "commit", "-m", f"release({args.environment}): {bundle['bundle_sha']}", cwd=work)
                pushed = git("push", args.remote, f"HEAD:refs/heads/{args.branch}", cwd=work, check=False)
                if pushed.returncode == 0:
                    print(bundle["bundle_sha"])
                    return
            finally:
                git("worktree", "remove", "--force", str(work))
    raise RuntimeError("release push failed after four fresh-checkout attempts")


if __name__ == "__main__":
    main()

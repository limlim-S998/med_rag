#!/usr/bin/env python3
"""Set the image tag in a chart's values file. This is the pipeline's last act.

The delivery model in one sentence: the pipeline builds an image, pushes it
tagged with the git SHA, and then *commits a change to a values file*. It does
not deploy. Flux deploys, by reconciling the cluster to that commit.

This used to be a `yq -i '.image.tag = "..."'` line inside
deploy/azure-pipelines/. Three problems with that:

  1. `yq` was not installed anywhere in this repo - not in the dev
     requirements, not in the images, not on a developer machine. The one step
     that performs a release could only ever run inside Azure Pipelines.
  2. It could not be tested. The single most consequential line in the
     delivery path had no coverage at all.
  3. It could not be dry-run. "What would this release change" had no answer
     short of running it.

Deliberately line-based rather than a YAML round-trip: the values files carry
the reasoning for every setting in comments, and most YAML libraries discard
comments on write. A release process that silently strips the explanation of
what it is releasing is not an improvement on doing it by hand.

    python scripts/bump_image_tag.py --service retrieval --tag 4f2a91c
    python scripts/bump_image_tag.py --all --tag $(git rev-parse HEAD) --dry-run
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHARTS = ROOT / "deploy" / "charts"

# Matches the tag line inside the image block, capturing indent, value and any
# trailing comment so all three survive the rewrite.
TAG_LINE = re.compile(r"^(?P<indent>\s*)tag:(?P<space>\s*)(?P<value>\S+)(?P<comment>.*)$")

# A tag must be a git SHA (short or full) or PLACEHOLDER. Anything else -
# above all `latest` - makes "what is running" unanswerable and rollback
# impossible, which is the property the whole delivery design is built on.
VALID_TAG = re.compile(r"^(?:[0-9a-f]{7,40}|PLACEHOLDER)$")

# Charts whose image is a third-party release rather than something we build.
# Qdrant is pinned to an upstream version and must never be given a git SHA.
NOT_OURS = {"qdrant"}


class BumpError(Exception):
    pass


def service_charts() -> list[str]:
    return sorted(
        d.name
        for d in CHARTS.iterdir()
        if d.is_dir() and d.name not in NOT_OURS and (d / "values.yaml").exists()
        # The library chart has no values of its own.
        and d.name != "medw-lib"
    )


def validate_tag(tag: str) -> None:
    if tag == "latest":
        raise BumpError(
            "refusing to set tag 'latest': it makes 'what is running' unanswerable "
            "and rollback impossible"
        )
    if not VALID_TAG.match(tag):
        raise BumpError(
            f"{tag!r} does not look like a git SHA. The image tag is the commit that "
            "built it - that is what makes the artifact traceable to source."
        )


def bump(service: str, tag: str, *, dry_run: bool = False) -> tuple[str, str]:
    """Rewrite the tag. Returns (old, new). Raises if the file is not as expected."""
    values = CHARTS / service / "values.yaml"
    if not values.exists():
        raise BumpError(f"no values.yaml for service {service!r}")

    lines = values.read_text().splitlines(keepends=True)
    in_image_block = False
    hits: list[int] = []

    for i, line in enumerate(lines):
        if re.match(r"^image:\s*$", line):
            in_image_block = True
            continue
        # A non-indented, non-blank line ends the block.
        if in_image_block and line.strip() and not line.startswith((" ", "\t")):
            in_image_block = False
        if in_image_block and TAG_LINE.match(line):
            hits.append(i)

    if len(hits) != 1:
        raise BumpError(
            f"expected exactly one image.tag line in {values.relative_to(ROOT)}, found {len(hits)}"
        )

    i = hits[0]
    m = TAG_LINE.match(lines[i])
    assert m is not None
    old = m.group("value")
    lines[i] = f"{m['indent']}tag:{m['space']}{tag}{m['comment']}\n"

    if not dry_run:
        values.write_text("".join(lines))
    return old, tag


def git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--service", help="chart directory name, e.g. retrieval")
    target.add_argument("--all", action="store_true", help="every service chart we build")
    p.add_argument("--tag", help="git SHA. Defaults to HEAD.")
    p.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = p.parse_args(argv)

    tag = args.tag or git_sha()
    try:
        validate_tag(tag)
        services = service_charts() if args.all else [args.service]
        for svc in services:
            old, new = bump(svc, tag, dry_run=args.dry_run)
            verb = "would set" if args.dry_run else "set"
            print(f"{verb} {svc}: {old} -> {new}")
    except BumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

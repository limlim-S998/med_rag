#!/usr/bin/env python3
"""Create, validate and select immutable releases without contacting a cluster."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re

import yaml

from medw_core.content import prompt_hash

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICES = ("gateway", "retrieval", "generation", "ingestion-worker", "reranker")
ENVIRONMENTS = ("dev", "staging", "prod")
SHA = re.compile(r"(?!0{40}$)[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:(?!0{64}$)[0-9a-f]{64}")
MODEL_KEYS = ("chat_model_name", "chat_model_version", "embed_model_name", "embed_model_version")
BEHAVIOR_KEYS = {*MODEL_KEYS, "embed_version", "embed_dim", "prompt_bundle_sha", "temperature", "rrf_k",
                 "fusion_top_n", "rerank_top_k", "search_ef", "table_classifier_version"}


def content_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate(bundle: dict) -> dict:
    if bundle.get("schema_version") != 1:
        raise ValueError("unsupported release schema")
    if not SHA.fullmatch(bundle.get("chart_source_sha", "")):
        raise ValueError("full chart source revision required")
    if set(bundle.get("images", {})) != set(SERVICES):
        raise ValueError("release must contain exactly all five service images")
    for service, image in bundle["images"].items():
        if not DIGEST.fullmatch(image.get("digest", "")):
            raise ValueError(f"{service}: immutable image digest required")
        if not SHA.fullmatch(image.get("source_sha", "")):
            raise ValueError(f"{service}: full source revision required")
    behavior = bundle.get("behavior", {})
    dimension = behavior.get("embed_dim")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("embed_dim must be a positive integer")
    unknown = set(behavior) - BEHAVIOR_KEYS
    if unknown:
        raise ValueError(f"target-specific or unknown behavior keys: {sorted(unknown)}")
    for key in (*MODEL_KEYS, "embed_version"):
        if not isinstance(behavior.get(key), str) or not behavior[key].strip():
            raise ValueError(f"expected model identity missing: {key}")
        if key.endswith("_version") and behavior[key].lower() in {"latest", "main", "master"}:
            raise ValueError(f"floating model identity: {key}")
        if any(word in behavior[key].lower() for word in ("placeholder", "unknown", "unversioned")):
            raise ValueError(f"placeholder identity: {key}")
    if not DIGEST.fullmatch(behavior.get("prompt_bundle_sha", "")):
        raise ValueError("content-derived prompt hash required")
    payload = {k: v for k, v in bundle.items() if k != "bundle_sha"}
    if bundle.get("bundle_sha") != content_hash(payload):
        raise ValueError("release bundle hash disagrees with contents")
    return bundle


def create(images: dict, behavior: dict, prompts: pathlib.Path, *,
           chart_source_sha: str | None = None) -> dict:
    behavior = {**behavior, "prompt_bundle_sha": prompt_hash(prompts)}
    bundle = {"schema_version": 1, "images": images, "behavior": behavior,
              "chart_source_sha": chart_source_sha or images["generation"]["source_sha"]}
    bundle["bundle_sha"] = content_hash(bundle)
    return validate(bundle)


def release_patches(bundle: dict) -> list[dict]:
    validate(bundle)
    patches = [{
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": service, "namespace": "medw"},
        "spec": {"suspend": False,
                 "chart": {"spec": {"sourceRef": {"name": "medwriter-release-charts"}}},
                 "values": {
            "releaseRequired": True,
            "image": {"digest": image["digest"], "sourceSha": image["source_sha"]},
            "config": {**copy.deepcopy(bundle["behavior"]),
                       "release_bundle_sha": bundle["bundle_sha"]},
        }},
    } for service, image in bundle["images"].items()]
    # Backups reuse the already built ingestion image and its Azure Blob client.
    worker = bundle["images"]["ingestion-worker"]
    patches.append({"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
                    "metadata": {"name": "qdrant", "namespace": "medw"},
                    "spec": {"chart": {"spec": {"sourceRef": {"name": "medwriter-release-charts"}}},
                             "values": {"snapshots": {"image": {
                        "digest": worker["digest"], "sourceSha": worker["source_sha"]}}}}})
    patches.append({"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                    "metadata": {"name": "medwriter-release-charts", "namespace": "flux-system"},
                    "spec": {"suspend": False, "ref": {"commit": bundle["chart_source_sha"]}}})
    return patches


def select(bundle: dict, environment: str, *, root: pathlib.Path = ROOT) -> pathlib.Path:
    validate(bundle)
    if environment not in ENVIRONMENTS:
        raise ValueError(f"unknown release environment: {environment}")
    target = root / "deploy" / "flux" / environment / "release-values.yaml"
    if not target.parent.exists():
        raise ValueError(f"missing environment directory: {target.parent}")
    target.write_text(yaml.safe_dump_all(release_patches(bundle), sort_keys=False))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_cmd = commands.add_parser("create")
    create_cmd.add_argument("--images", type=pathlib.Path, required=True)
    create_cmd.add_argument("--behavior", type=pathlib.Path, required=True)
    create_cmd.add_argument("--prompts", type=pathlib.Path,
                            default=ROOT / "services/generation/app/prompts")
    create_cmd.add_argument("--output", type=pathlib.Path, required=True)
    create_cmd.add_argument("--chart-source-sha", help="chart-only release: select an existing Git revision")
    for command in ("validate", "select"):
        cmd = commands.add_parser(command)
        cmd.add_argument("bundle", type=pathlib.Path)
        if command == "select":
            cmd.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    args = parser.parse_args()
    if args.command == "create":
        bundle = create(json.loads(args.images.read_text()), json.loads(args.behavior.read_text()),
                        args.prompts, chart_source_sha=args.chart_source_sha)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    else:
        bundle = validate(json.loads(args.bundle.read_text()))
        if args.command == "select":
            print(select(bundle, args.environment))
    print(bundle["bundle_sha"])


if __name__ == "__main__":
    main()

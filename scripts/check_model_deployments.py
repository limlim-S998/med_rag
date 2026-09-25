#!/usr/bin/env python3
"""Validate target infrastructure and the currently installed model identities.

Remote Azure adapters remain available, but are not the installed implementations.
Their ARM validation belongs to the release that actually wires those adapters.
"""
import argparse
import ast
import json
import pathlib
import uuid
from string import Template

import yaml

if __package__:
    from .release import ROOT, validate
else:
    from release import ROOT, validate



REQUIRED_TARGETS = {
    "gateway": ("cosmos_endpoint", "blob_account_url", "sql_server", "auth_tenant_id",
                "auth_audience", "auth_issuer", "auth_jwks_url"),
    "retrieval": ("cosmos_endpoint", "search_endpoint"),
    "generation": ("cosmos_endpoint", "blob_account_url", "sql_server", "retrieval_url",
                   "auth_tenant_id", "auth_audience", "auth_issuer", "auth_jwks_url"),
    "ingestion-worker": ("cosmos_endpoint", "blob_account_url", "sql_server", "search_endpoint"),
    "reranker": (),
}


def validate_targets(documents: list[dict]) -> dict:
    """Reject unprepared target configuration before any cloud reads/migrations."""
    settings = {}
    by_name = {doc["metadata"]["name"]: doc for doc in documents}
    for name, required in REQUIRED_TARGETS.items():
        values = by_name[name]["spec"]["values"]
        config = values.get("config", {})
        for key in required:
            if (not isinstance(config.get(key), str) or not config[key].strip()
                    or "${" in config[key] or "unconfigured" in config[key]):
                raise ValueError(f"{name}: target setting {key} must be configured")
        if name != "reranker":
            identity = values.get("serviceAccount", {}).get("annotations", {}).get(
                "azure.workload.identity/client-id", "")
            if not identity or uuid.UUID(identity).int == 0:
                raise ValueError(f"{name}: workload identity must be configured")
        secrets = {env["name"]: env.get("secretKeyRef", {}) for env in values.get("secretEnv", [])}
        if secrets.get("MEDW_APPINSIGHTS_CONNECTION_STRING") != {
            "name": "medw-telemetry", "key": "connection-string"
        }:
            raise ValueError(f"{name}: medw-telemetry/connection-string reference required")
        if not values.get("serviceMonitor", {}).get("enabled"):
            raise ValueError(f"{name}: ServiceMonitor must be enabled")
        settings[name] = config
    return settings

def installed_identities() -> dict:
    tree = ast.parse((ROOT / "libs/medw_core/placeholders.py").read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "MODEL_IDENTITIES"
                for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise ValueError("installed implementations have no model identity declaration")


def validate_installed(behavior: dict, identities: dict) -> None:
    for prefix, kind in (("chat", "chat"), ("embed", "embedding")):
        for field in ("name", "version"):
            if behavior[f"{prefix}_model_{field}"] != identities[kind][field]:
                raise ValueError(f"{kind} identity differs from installed implementation")
    if behavior["embed_version"] != identities["embedding"]["deployment"]:
        raise ValueError("embedding compatibility differs from installed implementation")
    if behavior["table_classifier_version"] != identities["classifier"]["version"]:
        raise ValueError("classifier identity differs from installed implementation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    parser.add_argument("--environment", choices=("dev", "staging", "prod"), required=True)
    args = parser.parse_args()
    bundle = validate(json.loads(args.bundle.read_text()))
    environment = ROOT / "deploy/flux" / args.environment / "environment-values.yaml"
    platform = ROOT / "deploy/flux/clusters" / args.environment / "platform-config.yaml"
    values = yaml.safe_load(platform.read_text())["data"]
    settings = validate_targets(list(yaml.safe_load_all(Template(environment.read_text()).substitute(values))))
    identities = installed_identities()
    validate_installed(bundle["behavior"], identities)
    for service, kind, prefix in (("generation", "chat", "chat"),
                                  ("retrieval", "embedding", "embed"),
                                  ("ingestion-worker", "embedding", "embed")):
        if settings[service].get(f"{prefix}_deployment") != identities[kind]["deployment"]:
            raise ValueError(f"{service}: deployment differs from installed implementation")
    print("verified installed model identities and configured Azure infrastructure targets")


if __name__ == "__main__":
    main()

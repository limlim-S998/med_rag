#!/usr/bin/env python3
"""Read expected Azure model identities through management metadata, without inference."""
import argparse
import json
import pathlib
import subprocess
import uuid
from urllib.parse import quote

import yaml

from medw_core.model_identity import validate_deployment

if __package__:
    from .release import ROOT, validate
else:
    from release import ROOT, validate



REQUIRED_TARGETS = {
    "gateway": ("cosmos_endpoint", "blob_account_url", "sql_server", "auth_tenant_id",
                "auth_audience", "auth_issuer", "auth_jwks_url"),
    "retrieval": ("aoai_endpoint", "aoai_resource_id", "cosmos_endpoint", "search_endpoint"),
    "generation": ("aoai_endpoint", "aoai_resource_id", "cosmos_endpoint", "blob_account_url",
                   "sql_server"),
    "ingestion-worker": ("aoai_endpoint", "aoai_resource_id", "cosmos_endpoint", "blob_account_url",
                         "sql_server", "search_endpoint", "docintel_endpoint", "language_endpoint"),
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
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError(f"{name}: target setting {key} must be configured")
        if config.get("backend") != "azure":
            raise ValueError(f"{name}: cloud release requires the Azure backend")
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

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    parser.add_argument("--environment", choices=("dev", "staging", "prod"), required=True)
    args = parser.parse_args()
    bundle = validate(json.loads(args.bundle.read_text()))
    environment = ROOT / "deploy/flux" / args.environment / "environment-values.yaml"
    settings = validate_targets(list(yaml.safe_load_all(environment.read_text())))
    for service, kind in (("generation", "chat"), ("retrieval", "embed"),
                          ("ingestion-worker", "embed")):
        config = settings[service]
        resource = config.get("aoai_resource_id", "")
        deployment = config.get(f"{kind}_deployment", "")
        if not resource.startswith("/subscriptions/") or not deployment:
            raise ValueError(f"{service}: configure resource ID and deployment before release")
        url = (f"https://management.azure.com{resource}/deployments/{quote(deployment, safe='')}"
               "?api-version=2024-10-01")
        result = subprocess.run(["az", "rest", "--method", "get", "--url", url, "--output", "json"],
                                check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        expected = (bundle["behavior"][f"{kind}_model_name"],
                    bundle["behavior"][f"{kind}_model_version"])
        validate_deployment(payload, *expected)
        print(f"verified {service} {kind}: {expected[0]} {expected[1]}")


if __name__ == "__main__":
    main()

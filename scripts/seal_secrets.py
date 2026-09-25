#!/usr/bin/env python3
"""Initialize SOPS-encrypted application secrets once; retain keys on reruns."""
from __future__ import annotations

import argparse
import base64
import pathlib
import secrets
import subprocess

import yaml


def secret(name: str, values: dict) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {
        "name": name, "namespace": "medw"}, "type": "Opaque", "stringData": values}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-url", required=True, help="Versioned SOPS key URL exported by bootstrap Terraform")
    parser.add_argument("--directory", type=pathlib.Path, default=pathlib.Path("deploy/flux/secrets/dev"))
    parser.add_argument("--telemetry-file", type=pathlib.Path, required=True,
                        help="Ignored file containing Terraform's sensitive telemetry_connection_string output")
    parser.add_argument("--external-postgres-url-file", type=pathlib.Path,
                        help="Optional existing PostgreSQL URI, including sslmode=verify-full")
    parser.add_argument("--refresh-telemetry", action="store_true")
    args = parser.parse_args()
    output = args.directory / "secrets.enc.yaml"
    telemetry_value = args.telemetry_file.read_text().strip()
    if not telemetry_value:
        parser.error("Telemetry connection string must not be empty")
    if output.exists():
        # Decryption errors never cause key regeneration or replacement.
        plain = subprocess.check_output(["sops", "--decrypt", str(output)], text=True)
        documents = list(yaml.safe_load_all(plain))
        required = {
            "qdrant-auth": {"api-key", "read-only-api-key"},
            "medw-airflow": {"postgres-password", "connection", "fernet-key", "jwt-secret",
                            "api-secret-key", "admin-password"},
            "medw-telemetry": {"connection-string"},
        }
        values = {d["metadata"]["name"]: d.get("stringData", {}) for d in documents}
        for name, keys in required.items():
            if any(not values.get(name, {}).get(key) for key in keys):
                parser.error(f"Existing encrypted {name} is incomplete; restore it rather than regenerate keys")
        if not args.refresh_telemetry:
            print("Existing secrets decrypt successfully; keys retained")
            return
        telemetry = next(d for d in documents if d["metadata"]["name"] == "medw-telemetry")
        telemetry["stringData"]["connection-string"] = telemetry_value
    else:
        password = secrets.token_urlsafe(32)
        connection = f"postgresql://airflow:{password}@airflow-db:5432/airflow"
        if args.external_postgres_url_file:
            connection = args.external_postgres_url_file.read_text().strip()
            if not connection.startswith(("postgresql://", "postgres://")) or "sslmode=verify-full" not in connection:
                parser.error("External PostgreSQL requires a PostgreSQL URI with sslmode=verify-full")
        documents = [
            secret("qdrant-auth", {"api-key": secrets.token_urlsafe(32),
                                  "read-only-api-key": secrets.token_urlsafe(32)}),
            secret("medw-airflow", {
                "postgres-password": password, "connection": connection,
                "fernet-key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                "jwt-secret": secrets.token_urlsafe(64), "api-secret-key": secrets.token_urlsafe(32),
                "admin-password": secrets.token_urlsafe(32)}),
            secret("medw-telemetry", {"connection-string": telemetry_value}),
        ]
    encrypted = subprocess.check_output([
        "sops", "--encrypt", "--azure-kv", args.key_url, "--encrypted-regex", "^(data|stringData)$",
        "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin",
    ], input=yaml.safe_dump_all(documents, sort_keys=False), text=True)
    # Never put plaintext in a temporary file, a command argument or stdout.
    temporary = output.with_suffix(".tmp")
    temporary.write_text(encrypted)
    temporary.replace(output)
    kustomization = args.directory / "kustomization.yaml"
    config = yaml.safe_load(kustomization.read_text())
    if output.name not in config["resources"]:
        config["resources"].append(output.name)
        kustomization.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"Encrypted application secrets: {output}")


if __name__ == "__main__":
    main()

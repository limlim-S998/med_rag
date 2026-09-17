"""Airflow's private application client. No human login or storage credentials."""

import hashlib
import os
from urllib.parse import urlsplit

import requests
from azure.identity import WorkloadIdentityCredential


class BatchClient:
    def __init__(self):
        self.url = os.environ["MEDW_INGESTION_URL"].rstrip("/")
        endpoint = urlsplit(self.url)
        if endpoint.scheme not in {"http", "https"} or endpoint.username or endpoint.query:
            raise ValueError("invalid configured ingestion service URL")
        # AKS injects the federated service-account token; no operator fallback.
        self.credential = WorkloadIdentityCredential()
        self.scope = "api://" + os.environ["MEDW_AUTH_AUDIENCE"] + "/.default"
        self.http = requests.Session()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()
        self.credential.close()

    def _request(self, method: str, path: str, *, correlation_id: str, body=None) -> dict:
        token = self.credential.get_token(self.scope)
        response = self.http.request(
            method, self.url + path, json=body, timeout=(5, 120), allow_redirects=False,
            headers={"Authorization": "Bearer " + token.token, "X-Correlation-ID": correlation_id})
        if not 200 <= response.status_code < 300:
            # Do not expose response bodies or token-bearing headers in Airflow.
            raise RuntimeError(f"batch API returned HTTP {response.status_code}")
        return response.json()

    def prepare(self, run_id: str, cutoff: float) -> dict:
        correlation = hashlib.sha256(("ingest_study/" + run_id).encode()).hexdigest()[:32]
        return self._request("POST", "/_internal/batches", correlation_id=correlation,
                             body={"run_id": run_id, "cutoff": cutoff})

    def status(self, batch_id: str) -> dict:
        if len(batch_id) != 64 or any(c not in "0123456789abcdef" for c in batch_id):
            raise ValueError("invalid batch identifier")
        return self._request("GET", "/_internal/batches/" + batch_id,
                             correlation_id=batch_id[:32])

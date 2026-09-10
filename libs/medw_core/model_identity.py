"""Read deployment metadata without sending an inference request.

Azure's deployment name is a label, not proof of the underlying model version.
https://learn.microsoft.com/rest/api/aiservices/accountmanagement/deployments/get
"""

import asyncio
import time
from urllib.parse import quote


def validate_deployment(payload: dict, expected_name: str, expected_version: str) -> None:
    properties = payload.get("properties", {})
    model = properties.get("model", {})
    if not expected_name or not expected_version:
        raise ValueError("model name and version must be pinned")
    if (model.get("name"), model.get("version")) != (expected_name, expected_version):
        raise ValueError("deployed model does not match release metadata")
    if properties.get("provisioningState") != "Succeeded":
        raise ValueError("model deployment is not provisioned")
    if properties.get("versionUpgradeOption") != "NoAutoUpgrade":
        raise ValueError("automatic model upgrades must be disabled")


class ModelIdentityCheck:
    def __init__(self, credential, http, resource_id: str,
                 expected: list[tuple[str, str, str]], *, cache_seconds: float = 60):
        self.credential, self.http, self.resource_id = credential, http, resource_id
        self.expected = expected
        self.cache_seconds = cache_seconds
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def check(self) -> None:
        async with self.lock:
            if time.monotonic() < self.expires:
                return
            if not self.resource_id.startswith("/subscriptions/") or "/accounts/" not in self.resource_id:
                raise ValueError("Azure OpenAI resource ID is required for model verification")
            token = await self.credential.get_token("https://management.azure.com/.default")
            for deployment, name, version in self.expected:
                url = ("https://management.azure.com" + self.resource_id.rstrip("/")
                       + "/deployments/" + quote(deployment, safe=""))
                response = await self.http.get(
                    url, params={"api-version": "2024-10-01"},
                    headers={"Authorization": "Bearer " + token.token},
                )
                response.raise_for_status()
                validate_deployment(response.json(), name, version)
            self.expires = time.monotonic() + self.cache_seconds

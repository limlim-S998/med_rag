"""Deployment labels cannot substitute for underlying model metadata."""

from types import SimpleNamespace

import httpx
import pytest

from medw_core.model_identity import ModelIdentityCheck, validate_deployment


def metadata(**updates):
    return {"properties": {"model": {"name": "chat", "version": "2025-01-01"},
                           "provisioningState": "Succeeded",
                           "versionUpgradeOption": "NoAutoUpgrade", **updates}}


@pytest.mark.parametrize("payload", [
    metadata(model={"name": "chat", "version": "a-different-version"}),
    metadata(model={"name": "different-model", "version": "2025-01-01"}),
    metadata(provisioningState="Failed"),
    metadata(versionUpgradeOption="OnceNewDefaultVersionAvailable"),
])
def test_deployment_drift_fails_validation(payload):
    with pytest.raises(ValueError):
        validate_deployment(payload, "chat", "2025-01-01")


async def test_readiness_uses_cached_metadata_not_inference():
    class Credential:
        async def get_token(self, scope):
            assert scope == "https://management.azure.com/.default"
            return SimpleNamespace(token="synthetic-test-token")

    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=metadata())

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        check = ModelIdentityCheck(Credential(), http,
                                   "/subscriptions/test/resourceGroups/test/providers/"
                                   "Microsoft.CognitiveServices/accounts/test",
                                   [("named-deployment", "chat", "2025-01-01")])
        await check.check()
        await check.check()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "management.azure.com"
    assert requests[0].url.path.endswith("/deployments/named-deployment")

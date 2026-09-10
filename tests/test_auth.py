"""Authentication is verified with generated keys, never a live identity provider."""

import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException

from medw_core.auth import TokenValidator, study_user
from medw_core.local.platform import LocalStudyAccess
from medw_core.persistence import SQLiteStateStore
from medw_core.settings import Settings


@pytest.fixture
def signed():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="signing-key", alg="RS256", use="sig")
    now = int(time.time())
    claims = {"iss": "https://identity.test/tenant/v2.0", "aud": "medw-api",
              "tid": "tenant", "oid": "writer", "iat": now, "nbf": now - 1,
              "exp": now + 600, "roles": ["writer"]}
    return key, jwk, claims


def config():
    return Settings(backend="local", env="test", auth_tenant_id="tenant",
                    auth_audience="medw-api", auth_issuer="https://identity.test/tenant/v2.0",
                    auth_jwks_url="https://identity.test/keys")


def encode(key, claims):
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "signing-key"})


async def test_signed_identity_and_cached_keys(signed):
    key, jwk, claims = signed
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        validator = TokenValidator(config(), client)
        for _ in range(2):
            user = await validator.validate(encode(key, claims))
            assert user.oid == "writer" and user.tenant_id == "tenant"
    assert calls == ["https://identity.test/keys"]


@pytest.mark.parametrize("field,value", [
    ("iss", "https://attacker.test"), ("aud", "another-api"), ("tid", "another-tenant"),
    ("exp", 1), ("nbf", 9999999999), ("roles", "admin"), ("oid", ""),
])
async def test_invalid_claims_rejected(signed, field, value):
    key, jwk, claims = signed
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [jwk]}),
    )) as client:
        with pytest.raises(HTTPException) as error:
            await TokenValidator(config(), client).validate(encode(key, {**claims, field: value}))
        assert error.value.status_code == 401


async def test_wrong_signature_and_algorithm_rejected(signed):
    _, jwk, claims = signed
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [jwk]}),
    )) as client:
        validator = TokenValidator(config(), client)
        for token in (encode(other, claims), jwt.encode(claims, "x" * 32, algorithm="HS256")):
            with pytest.raises(HTTPException) as error:
                await validator.validate(token)
            assert error.value.status_code == 401


async def test_key_outage_fails_closed(signed):
    key, _, claims = signed
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(503),
    )) as client:
        with pytest.raises(HTTPException) as error:
            await TokenValidator(config(), client).validate(encode(key, claims))
        assert error.value.status_code == 503


async def test_study_access_precedes_domain_handler_and_survives_restart(signed, tmp_path):
    key, jwk, claims = signed
    state = SQLiteStateStore(tmp_path / "access.sqlite")
    access = LocalStudyAccess(state)
    await access.grant("writer", "allowed")
    await state.close()
    state = SQLiteStateStore(tmp_path / "access.sqlite")
    app = FastAPI()
    calls = []

    @app.post("/studies/{study_id}/draft", dependencies=[Depends(study_user)])
    async def draft(study_id: str):
        calls.append(study_id)
        return {"synthetic": True}

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [jwk]}),
    )) as keys:
        app.state.token_validator = TokenValidator(config(), keys)
        app.state.services = SimpleNamespace(authorization=LocalStudyAccess(state))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://gateway") as client:
            assert (await client.post("/studies/allowed/draft")).status_code == 401
            headers = {"Authorization": "Bearer " + encode(key, claims)}
            assert (await client.post("/studies/other/draft", headers=headers)).status_code == 403
            assert calls == []
            assert (await client.post("/studies/allowed/draft", headers=headers)).status_code == 200
            await app.state.services.authorization.revoke("writer", "allowed")
            assert (await client.post("/studies/allowed/draft", headers=headers)).status_code == 403
    assert calls == ["allowed"]
    await state.close()

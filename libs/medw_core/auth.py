"""Validate gateway tokens against configured signing keys and study access.

The JWKS URL comes from deployment configuration, never token-supplied data.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from medw_core.settings import Settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    oid: str
    roles: tuple[str, ...] = ()
    tenant_id: str = ""


class TokenValidator:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.http = http
        self.tenant = settings.auth_tenant_id
        self.audience = settings.auth_audience
        self.issuer = settings.auth_issuer or (
            f"https://login.microsoftonline.com/{self.tenant}/v2.0" if self.tenant else ""
        )
        self.jwks_url = settings.auth_jwks_url or (
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys"
            if self.tenant else ""
        )
        self.cache_seconds = settings.auth_jwks_cache_seconds
        self.timeout = settings.readiness_timeout
        self.keys: dict[str, jwt.PyJWK] = {}
        self.expires = 0.0
        self.last_refresh = float("-inf")
        self.lock = asyncio.Lock()

    async def _refresh(self, *, unknown_key: bool = False) -> None:
        if not (self.tenant and self.audience and self.issuer and self.jwks_url):
            raise HTTPException(503, "authentication is not configured")
        async with self.lock:
            now = time.monotonic()
            if now < self.expires and (not unknown_key or now - self.last_refresh < 5):
                return
            try:
                response = await self.http.get(self.jwks_url, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                keys = {
                    key["kid"]: jwt.PyJWK.from_dict(key, algorithm="RS256")
                    for key in payload["keys"]
                    if key.get("kid") and key.get("kty") == "RSA"
                    and key.get("use", "sig") == "sig"
                    and key.get("alg", "RS256") == "RS256"
                }
                if not keys:
                    raise ValueError("no RSA signing keys")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
                raise HTTPException(503, "authentication keys unavailable") from exc
            self.keys = keys
            self.last_refresh = now
            self.expires = now + self.cache_seconds

    async def check(self) -> None:
        await self._refresh()

    async def validate(self, token: str) -> Principal:
        try:
            if len(token) > 16384:
                raise jwt.InvalidTokenError("oversized access token")
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise jwt.InvalidTokenError("unsupported signing algorithm or missing key ID")
            await self._refresh()
            kid = header["kid"]
            if kid not in self.keys:
                await self._refresh(unknown_key=True)
            if kid not in self.keys:
                raise jwt.InvalidTokenError("unknown signing key")
            claims = jwt.decode(
                token, self.keys[kid], algorithms=["RS256"],
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid"]},
            )
            if claims["tid"] != self.tenant or not isinstance(claims["oid"], str) or not claims["oid"]:
                raise jwt.InvalidTokenError("invalid identity")
            roles = claims.get("roles", [])
            if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
                raise jwt.InvalidTokenError("invalid roles")
            return Principal(oid=claims["oid"], roles=tuple(roles), tenant_id=claims["tid"])
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise HTTPException(401, "invalid access token", headers={"WWW-Authenticate": "Bearer"}) from exc


async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    validator = getattr(request.app.state, "token_validator", None)
    if validator is None:
        raise HTTPException(503, "authentication unavailable")
    return await validator.validate(creds.credentials)


async def study_user(
    study_id: str, request: Request, user: Principal = Depends(current_user),
) -> Principal:
    services = getattr(request.app.state, "services", None)
    access = getattr(services, "authorization", None)
    if access is None:
        raise HTTPException(503, "study authorization unavailable")
    try:
        allowed = await access.allowed(user.oid, study_id)
    except Exception as exc:
        raise HTTPException(503, "study authorization unavailable") from exc
    if not allowed:
        raise HTTPException(403, "study access denied")
    return user

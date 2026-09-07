# Microsoft Entra ID (AAD) token validation. Lives at the gateway only -
# internal services trust the cluster network and the correlation header, and
# re-validating a JWT on every internal hop buys nothing but latency.
#
# The two claims that matter downstream:
#   oid    the user's object ID. Goes into every audit row. NOT the email -
#          emails get reassigned, an oid does not.
#   roles  app roles from the app registration: writer, reviewer, admin. The
#          study-level authorisation check ("may this user see ABC-101") is a
#          separate lookup against the relational store, because it is data,
#          not a claim.

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer()


class Principal:
    oid: str
    roles: list[str]


async def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> Principal:
    # Validate signature against the tenant JWKS (cached), issuer, audience,
    # and expiry. Raise 401 on any failure - never fall through to anonymous.
    ...
    raise HTTPException(status_code=401)

"""NGINX asks for an access decision without sending the request body.

The edge overwrites both X-Original-* headers and never publicly routes this
endpoint. Study access stays here, under the gateway's existing SQL identity.
"""

import re
from typing import Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from medw_core.auth import Principal, current_user, study_user

router = APIRouter(prefix="/_internal/authorize", include_in_schema=False)
ROUTES = {
    "jobs": ("GET", r"/studies/([^/]+)/jobs/[^/]+"),
    "search": ("POST", r"/studies/([^/]+)/search"),
    "draft": ("POST", r"/studies/([^/]+)/sections/[^/]+/draft"),
    "ingest": ("POST", r"/studies/([^/]+)/documents/[^/]+/ingest"),
}


@router.get("/{operation}", status_code=204)
async def authorize(
    operation: Literal["jobs", "search", "draft", "ingest"], request: Request,
    user: Principal = Depends(current_user),
) -> Response:
    method, pattern = ROUTES[operation]
    raw_path = request.headers.get("x-original-uri", "").split("?", 1)[0]
    try:
        path = unquote(raw_path, errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(403, "invalid request path") from exc
    # Reject ambiguous encodings/normalization before checking membership. The
    # study authorized here must be the study the backend actually receives.
    if (path.count("/") != raw_path.count("/") or "%" in path or "\\" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise HTTPException(403, "invalid request path")
    match = re.fullmatch(pattern, path)
    if match is None or request.headers.get("x-original-method") != method:
        raise HTTPException(403, "request is outside the authorized route")
    await study_user(match[1], request, user)
    return Response(status_code=204)

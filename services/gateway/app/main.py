"""Writer operations and the internal access-decision endpoint used by NGINX."""
from fastapi import Depends, FastAPI, Request, Response

from medw_core.auth import Principal, current_user, study_user
from medw_core.service import (
    add_platform_routes,
    attach_request_instrumentation,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

from .routes import authorization, documents, draft

s = get_settings().model_copy(update={"service_name": "gateway"})
app = FastAPI(title="gateway", lifespan=lifespan_for("gateway", s))

# Attach request wrappers here; lifespan configures telemetry exporters once.
attach_request_instrumentation(app, "gateway")
add_platform_routes(app, s)

# Backend request/response bodies travel through NGINX. This app retains the
# access decision and writer operations that need its own dependencies.
app.include_router(authorization.router)
for router in (draft.router, documents.router):
    app.include_router(router, dependencies=[Depends(study_user)])


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


@app.get("/me")
async def me(user: Principal = Depends(current_user)) -> dict:
    return {"oid": user.oid, "roles": user.roles}

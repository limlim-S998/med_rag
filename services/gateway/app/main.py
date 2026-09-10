"""Public boundary: authenticate, authorize and expose platform health."""
from fastapi import Depends, FastAPI, Request, Response

from medw_core.auth import Principal, current_user, study_user
from medw_core.composition import effective_settings
from medw_core.service import add_platform_routes, instrument, lifespan_for, readiness_response
from medw_core.settings import get_settings

from .routes import documents, draft, search

s = effective_settings(get_settings().model_copy(update={"service_name": "gateway"}))
app = FastAPI(title="gateway", lifespan=lifespan_for("gateway", s))
instrument(app, s, "gateway")
add_platform_routes(app, s)
for router in (search.router, draft.router, documents.router):
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

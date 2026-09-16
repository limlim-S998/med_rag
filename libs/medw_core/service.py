"""Shared HTTP platform surfaces; no medical handlers live here."""

import asyncio
import dataclasses
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from medw_core import metrics, tracing
from medw_core.composition import build
from medw_core.content import prompt_hash
from medw_core.health import DependencyUnavailable, HealthMonitor, http_check, unavailable_check
from medw_core.provenance import Provenance
from medw_core.settings import Settings
from medw_core.telemetry import configure_telemetry


def lifespan_for(name: str, settings: Settings, *, vector_factory=None, sparse_factory=None,
                 prompts: Path | None = None):
    """Build one startup/shutdown context for either backend.

    FastAPI serves requests during the yield. Leaving the context on shutdown
    closes every client registered with AsyncExitStack, including on failures.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        actual_prompt = prompt_hash(prompts) if prompts is not None else settings.prompt_bundle_sha
        runtime_settings = settings.model_copy(
            update={"prompt_bundle_sha": actual_prompt, "service_name": name})
        app.state.provenance = Provenance.from_settings(runtime_settings)
        configure_telemetry(settings, app.state.provenance)
        app.state.settings = settings

        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=settings.readiness_timeout),
                event_hooks={"request": [tracing.httpx_request_hook]},
            ))
            app.state.http = http
            monitor = HealthMonitor(timeout=settings.readiness_timeout,
                                    cache_seconds=settings.readiness_cache_seconds)
            app.state.health = monitor
            if settings.backend == "azure" and (
                settings.build_source_sha == "unversioned"
                or settings.build_source_sha != settings.image_sha
            ):
                monitor.add("build-identity", unavailable_check("configured source differs from image"))
            app.state.services = None
            try:
                services = await build(settings, stack, service=name,
                                       vector_factory=vector_factory, sparse_factory=sparse_factory)
                app.state.services = services
                monitor.add("service-dependencies", services.require("health").check)
            except Exception:
                logging.getLogger(__name__).exception("service dependency initialization failed")
                monitor.add("configuration", unavailable_check("dependency initialization failed"))

            if name in {"gateway", "generation"}:
                from medw_core.auth import TokenValidator
                app.state.token_validator = TokenValidator(settings, http)
                # Synthetic diagnostics can run without an identity provider;
                # every writer route still enforces normal authentication.
                if not settings.synthetic_enabled:
                    monitor.add("identity-provider", app.state.token_validator.check)
                # Backend availability is independent of the access decision.
                # NGINX selects ready backend pods; a backend outage must not
                # withdraw the gateway and disable all study authorization.
            if name == "retrieval":
                monitor.add("reranker", http_check(http, f"{settings.reranker_url}/readyz"))
            if name == "generation":
                monitor.add("retrieval", http_check(http, f"{settings.retrieval_url}/readyz"))
            if name == "ingestion-worker" and app.state.services is not None:
                from medw_core.ingestion import IngestionRunner
                services = app.state.services
                app.state.ingestion = IngestionRunner(
                    settings, services, dense=services.require("dense_writer"),
                    sparse=services.require("sparse_writer"))
                worker = asyncio.create_task(app.state.ingestion.serve(), name="ingestion-poller")

                async def stop_worker():
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

                async def check_worker():
                    if worker.done():
                        raise RuntimeError("ingestion polling loop stopped")

                stack.push_async_callback(stop_worker)
                monitor.add("ingestion-poller", check_worker)
            if prompts is not None:
                if settings.prompt_bundle_sha.startswith("sha256:"):
                    if settings.prompt_bundle_sha != actual_prompt:
                        monitor.add("prompt-content", unavailable_check("prompt digest mismatch"))
                elif settings.backend == "azure":
                    monitor.add("prompt-content", unavailable_check("prompt digest is not pinned"))
                # Provenance reports disk content; a configured mismatch fails
                # readiness rather than attributing work to an unobserved hash.
            yield
    return lifespan


def attach_request_instrumentation(app: FastAPI, name: str) -> None:
    """Attach ASGI wrappers; exporter/provider configuration happens in lifespan."""
    app.add_middleware(metrics.InFlightMiddleware, service=name)
    app.add_middleware(tracing.TraceMiddleware, service=name)


def add_platform_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        body, content_type = metrics.render_prometheus()
        return Response(content=body, headers={"content-type": content_type})

    @app.get("/version")
    async def version(request: Request) -> dict:
        provenance = getattr(request.app.state, "provenance", None)
        data = dataclasses.asdict(provenance) if provenance is not None else {
            "image_sha": settings.image_sha,
            "image_digest": settings.image_digest,
            "release_bundle_sha": settings.release_bundle_sha,
            "backend": settings.backend,
        }
        return {**data, "backend": settings.backend, "medical_handlers": "placeholders"}

    if settings.synthetic_enabled:
        # Settings restricts this surface to explicitly opted-in local/test
        # environments. Nothing is parsed, generated or written to client data.
        @app.get("/_synthetic/work", include_in_schema=False)
        async def synthetic_work(seconds: float = Query(default=1, ge=0, le=30)):
            async def stream():
                yield json.dumps({"synthetic": True, "state": "started"}) + "\n"
                await asyncio.sleep(seconds)
                yield json.dumps({"synthetic": True, "state": "finished"}) + "\n"
            return StreamingResponse(stream(), media_type="application/x-ndjson")


async def readiness_response(request: Request) -> Response:
    monitor = getattr(request.app.state, "health", None)
    if monitor is None:
        return Response(status_code=503, headers={"x-readiness-reason": "initializing"})
    try:
        await monitor.check()
    except DependencyUnavailable as exc:
        return Response(status_code=503, headers={"x-readiness-reason": str(exc)})
    return Response(status_code=200)


def domain_unavailable() -> None:
    raise HTTPException(501, "medical domain implementation is intentionally held back")

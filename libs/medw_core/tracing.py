"""W3C trace context across ASGI/httpx and bounded correlation IDs in JSON logs."""

import json
import logging
import re

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from medw_core.context import CORRELATION_ID, HEADER, new_id

__all__ = ["CORRELATION_ID", "HEADER", "new_id"]

_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_provider = None


def configure(service_name: str, connection_string: str = "", *,
              resource_attributes: dict | None = None, span_exporter=None):
    """Configure one process provider. Azure export is optional for local runs."""
    global _provider
    if _provider is None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create(
            {**(resource_attributes or {}), "service.name": service_name}))
        if span_exporter is None and connection_string:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

            span_exporter = AzureMonitorTraceExporter(connection_string=connection_string)
        if span_exporter is not None:
            _provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(_provider)
    return _provider


async def httpx_request_hook(request) -> None:
    """Install on every shared AsyncClient to preserve the caller's trace."""
    propagate.inject(request.headers)
    request.headers[HEADER] = CORRELATION_ID.get()


class TraceMiddleware:
    """One server span covers streaming; correlation context is always reset."""

    def __init__(self, app, service: str):
        self.app = app
        self.tracer = trace.get_tracer(f"medwriter.{service}")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in {
            "/metrics", "/healthz", "/readyz"
        }:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        candidate = headers.get(HEADER, "")
        cid = candidate if _SAFE_ID.fullmatch(candidate) else new_id()
        token = CORRELATION_ID.set(cid)
        # Paths can contain study/document identities. Route templates can be
        # attached by handlers if useful; raw paths and bodies are not exported.
        method = scope.get("method", "HTTP")
        try:
            with self.tracer.start_as_current_span(
                method, context=propagate.extract(headers), kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "medw.correlation_id": cid},
            ) as span:
                async def send_traced(message):
                    if message["type"] == "http.response.start":
                        response_headers = [(k, v) for k, v in message.get("headers", [])
                                            if k.lower() != HEADER.encode()]
                        message = {**message, "headers": [*response_headers,
                                                          (HEADER.encode(), cid.encode())]}
                        span.set_attribute("http.response.status_code", message["status"])
                        if message["status"] >= 500:
                            span.set_status(Status(StatusCode.ERROR))
                    await send(message)
                await self.app(scope, receive, send_traced)
        finally:
            CORRELATION_ID.reset(token)


class CorrelationFilter(logging.Filter):
    def filter(self, record):
        record.correlation_id = CORRELATION_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record):
        context = trace.get_current_span().get_span_context()
        payload = {"ts": self.formatTime(record), "lvl": record.levelname,
                   "svc": self.service, "cid": CORRELATION_ID.get(),
                   "trace_id": format(context.trace_id, "032x"), "msg": record.getMessage()}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, service: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

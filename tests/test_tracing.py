import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from medw_core import tracing


@pytest.mark.asyncio
async def test_http_hops_share_trace_and_reset_correlation_context():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen = []

    async def downstream(scope, receive, send):
        seen.append(tracing.CORRELATION_ID.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = tracing.TraceMiddleware(downstream, "downstream")
    wrapped.tracer = provider.get_tracer("downstream")

    async def upstream(scope, receive, send):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped),
                                    event_hooks={"request": [tracing.httpx_request_hook]}) as client:
            response = await client.get("http://downstream/work")
        await send({"type": "http.response.start", "status": response.status_code, "headers": []})
        await send({"type": "http.response.body", "body": response.content})

    app = tracing.TraceMiddleware(upstream, "upstream")
    app.tracer = provider.get_tracer("upstream")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.get("http://upstream/work", headers={tracing.HEADER: "synthetic-proof"})
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].context.trace_id == spans[1].context.trace_id
    assert spans[0].parent.span_id == spans[1].context.span_id
    assert seen == ["synthetic-proof"]
    assert response.headers[tracing.HEADER] == "synthetic-proof"
    assert tracing.CORRELATION_ID.get() == "-"
    provider.shutdown()


@pytest.mark.asyncio
async def test_trace_context_resets_after_failure():
    async def boom(scope, receive, send):
        assert tracing.CORRELATION_ID.get() != "-"
        raise RuntimeError("synthetic failure")

    app = tracing.TraceMiddleware(boom, "proof")
    with pytest.raises(RuntimeError):
        await app({"type": "http", "headers": []}, None, None)
    assert tracing.CORRELATION_ID.get() == "-"


def test_json_log_escapes_untrusted_quotes_and_newlines():
    record = logging.LogRecord("test", logging.INFO, __file__, 1,
                               'a "quoted"\nmessage', (), None)
    rendered = tracing.JsonFormatter("proof").format(record)
    assert json.loads(rendered)["msg"] == 'a "quoted"\nmessage'
    assert len(rendered.splitlines()) == 1

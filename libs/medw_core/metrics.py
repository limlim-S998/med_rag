# Application Insights, beyond latency and error rate.
#
# Latency and errors tell you the service is up. They do not tell you the
# system got worse, and an LLM system gets worse silently - a prompt bundle
# lands, or Azure rolls a model version, and the outputs shift with no error
# anywhere. So the signals that actually matter here are ML signals, and every
# one of them is dimensioned by the three version axes (image SHA, model
# deployment + version, prompt bundle SHA). A metric you cannot slice by
# version cannot tell you what changed.
#
# The last two are the early warning. A rise in numeric-fidelity failures or
# JSON validation retries means something moved underneath you.

from opentelemetry import metrics

meter = metrics.get_meter("medwriter")

# The one metric an autoscaler reads, and it is a different kind of thing from
# everything below it.
#
# The rest of this file is ANALYTICAL: cost, recall, drift - questions asked
# minutes or weeks later, over long retention, in App Insights. This one is
# OPERATIONAL: a KEDA ScaledObject queries it every few seconds and decides
# whether to add a pod. Stale by two minutes it is worse than useless, because
# it scales up after the burst and down before the next one.
#
# That difference is the whole argument for exporting to Prometheus as well as
# to Azure Monitor. See docs/adr and ROADMAP C-and-three-quarters.
#
# An UpDownCounter, not a Counter: it goes down when a request finishes. The
# Prometheus exporter renders it as a gauge, which is what `sum(...)` in the
# ScaledObject query expects.
#
# NAME DISCIPLINE: the OTel name `medw.inflight_requests` becomes
# `medw_inflight_requests` on Prometheus export - dots to underscores. The
# ScaledObject template builds its query as `medw_{{ .Values.autoscaling.metric }}`,
# so the values key `inflight_requests` and this name have to agree. They are
# checked against each other in tests/test_metrics.py, because a mismatch here
# does not error: the query returns no series and KEDA sits at minReplicas
# looking like an autoscaler that has decided nothing needs scaling.
inflight_requests = meter.create_up_down_counter(
    "medw.inflight_requests",
    description="requests currently in flight; the KEDA scaling signal")

tokens_per_request = meter.create_histogram(
    "medw.tokens_per_request", unit="token",
    description="prompt + completion tokens, dimensioned by section type")

cost_per_section = meter.create_histogram(
    "medw.cost_per_section", unit="USD",
    description="tokens priced by deployment; the number the sponsor asks about")

retrieval_hit_at_k = meter.create_histogram(
    "medw.retrieval_hit_at_k",
    description="online proxy for the offline golden-set recall@k")

reranker_score = meter.create_histogram(
    "medw.reranker_score",
    description="distribution shift here means the corpus or the query mix moved")

numeric_fidelity_failures = meter.create_counter(
    "medw.numeric_fidelity_failures",
    description="a numeral in the output that was not in the parsed table. Never zero-tolerable")

json_validation_retries = meter.create_counter(
    "medw.json_validation_retries",
    description="structured-output parse failures that needed the retry-with-error loop")


def configure(connection_string: str, service_name: str) -> None:
    # configure_azure_monitor auto-instruments FastAPI, httpx and the Azure
    # SDKs, so the correlation ID from tracing.py stitches the whole hop chain
    # into one end-to-end trace without per-call plumbing.
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=connection_string, service_name=service_name)


def configure_prometheus() -> None:
    """Add a Prometheus reader to the SAME meter provider.

    Not a second set of instruments. OpenTelemetry supports multiple readers on
    one provider, so every instrument defined above is available to both
    destinations - App Insights for analysis, /metrics for scraping - without
    anything being defined twice. Two definitions is how the two views drift.

    Safe to call alongside configure(): the Azure exporter and this one are
    independent readers.
    """
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider

    # No need to re-create the instruments. get_meter() before a provider is
    # installed returns a proxy that forwards to whatever is set later, so the
    # module-level instruments above are live once this runs. Verified rather
    # than assumed: an instrument that silently recorded nothing would produce
    # exactly the empty-query failure this metric exists to avoid.
    metrics.set_meter_provider(MeterProvider(metric_readers=[PrometheusMetricReader()]))


def render_prometheus() -> tuple[bytes, str]:
    """The body and content type for a /metrics endpoint.

    Lives here rather than in each service so the exposition format and the
    registry are decided once. Prometheus is strict about the content type;
    getting it wrong yields a scrape that succeeds and parses nothing.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


class InFlightMiddleware:
    """Pure ASGI middleware maintaining the in-flight gauge.

    ASGI rather than FastAPI's BaseHTTPMiddleware on purpose: BaseHTTPMiddleware
    wraps the response in a buffering layer that breaks streaming, and the
    generation service streams tokens. Middleware that quietly disabled
    streaming to count requests would be measuring the thing it broke.

    The decrement is in a finally block. If it were not, a handler that raised
    would leak a count, the gauge would drift upward forever, and the
    autoscaler would scale on a number that only goes up.
    """

    def __init__(self, app, service: str):
        self.app = app
        self.attrs = {"app": service}   # matches the ScaledObject's label filter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        inflight_requests.add(1, self.attrs)
        try:
            await self.app(scope, receive, send)
        finally:
            inflight_requests.add(-1, self.attrs)

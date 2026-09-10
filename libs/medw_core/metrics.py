# One provider supplies operational metrics and future analytical instruments.
# The in-flight signal is measured by middleware and exported to Prometheus
# for KEDA. Medical-quality instruments await the held-back domain handlers;
# declaring them does not establish measured recall or numerical fidelity.
# Release metadata belongs in telemetry resources/audit; operational labels
# exclude study, user and request identifiers. See README.md for the evidence.

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
# to Azure Monitor. See README.md#operational-signals-and-recovery.
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


def build_provider(service_name: str, connection_string: str = "", *,
                   metric_exporter=None, resource_attributes: dict | None = None):
    """Construct both readers together; the Azure SDK must not set a second provider.

    ``metric_exporter`` is an injectable export boundary for a local contract
    test. Production uses the real Azure exporter when a connection is supplied.
    Resource attributes hold release identity, never request/study identifiers.
    """
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    readers: list[MetricReader] = [PrometheusMetricReader()]
    if metric_exporter is None and connection_string:
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter

        metric_exporter = AzureMonitorMetricExporter(connection_string=connection_string)
    if metric_exporter is not None:
        readers.append(PeriodicExportingMetricReader(metric_exporter))
    return MeterProvider(
        metric_readers=readers,
        resource=Resource.create({**(resource_attributes or {}), "service.name": service_name}),
    )


_provider = None
_connection_string = ""


def configure(connection_string: str = "", service_name: str = "medwriter", *,
              resource_attributes: dict | None = None):
    """Initialize once per process, before serving requests, with all readers.

    Lifespan tests may enter the same application repeatedly. They reuse the
    process provider; module imports must not initialize a Prometheus-only one
    before the Azure connection string is known. OpenTelemetry shuts it down at
    process exit, flushing the periodic export reader.
    """
    global _provider, _connection_string
    if _provider is not None and connection_string and connection_string != _connection_string:
        raise RuntimeError("metrics was initialized before the Azure reader was configured")
    if _provider is None:
        _provider = build_provider(service_name, connection_string,
                                   resource_attributes=resource_attributes)
        metrics.set_meter_provider(_provider)
        _connection_string = connection_string
    return _provider


def configure_prometheus() -> None:
    """Compatibility for Prometheus-only tools; services call configure instead."""
    configure()


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

    # Paths that must not count towards load.
    #
    # /metrics is the scrape itself: the middleware increments, the handler
    # renders the gauge INCLUDING that increment, and the scrape records 1 on a
    # completely idle service. Measured on the cluster before this exclusion
    # existed - the query returned exactly 1 with no traffic.
    #
    # That is not cosmetic. KEDA divides the metric by the replica count and
    # compares to the target, so a permanent floor of one-per-pod is load the
    # autoscaler cannot distinguish from real work: with target 2 it eats half
    # the headroom, and a metric that never returns to zero can never scale
    # back down to minReplicas.
    #
    # The probes are excluded for the same reason at higher frequency: liveness
    # every 10s and readiness every 5s would otherwise be a constant baseline
    # that grows with how carefully you configured your probes.
    EXCLUDED = frozenset({"/metrics", "/healthz", "/readyz"})

    def __init__(self, app, service: str):
        self.app = app
        self.attrs = {"app": service}   # matches the ScaledObject's label filter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.EXCLUDED:
            return await self.app(scope, receive, send)
        inflight_requests.add(1, self.attrs)
        try:
            await self.app(scope, receive, send)
        finally:
            inflight_requests.add(-1, self.attrs)

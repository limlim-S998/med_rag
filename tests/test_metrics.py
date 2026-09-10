# The metric the autoscaler reads must exist, and be spelled the same way in
# three places that never see each other.
#
# The chain: values.yaml says `metric: inflight_requests`, the ScaledObject
# template builds `sum(medw_{that}{app="<name>"})`, and Python defines an
# instrument called `medw.inflight_requests` which Prometheus exports as
# `medw_inflight_requests`. Four hops, two languages, one dot-to-underscore
# conversion.
#
# It was broken: the query asked for a metric nothing emitted. Nothing errored.
# A PromQL query with no matching series returns empty, KEDA reads that as "no
# load", and the ScaledObject sits at minReplicaCount looking like an
# autoscaler that has considered the situation and decided against acting.

import pathlib
import re

import pytest
import yaml

from medw_core import metrics as m

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHARTS = ROOT / "deploy" / "charts"

# Instruments defined in Python, as Prometheus would name them.
PY_INSTRUMENTS = {
    name.replace(".", "_")
    for name in re.findall(r'"(medw\.[a-z_]+)"', (ROOT / "libs/medw_core/metrics.py").read_text())
}


def autoscaled_charts():
    out = []
    for d in sorted(CHARTS.iterdir()):
        vals = d / "values.yaml"
        if not vals.exists():
            continue
        auto = (yaml.safe_load(vals.read_text()) or {}).get("autoscaling") or {}
        if auto.get("enabled") and auto.get("metric") != "cpu":
            out.append((d.name, auto["metric"]))
    return out


@pytest.mark.parametrize("chart,metric", autoscaled_charts(), ids=lambda x: str(x))
def test_scaling_metric_is_actually_emitted(chart, metric):
    """The one that would have caught the original break.

    A chart scaling on `inflight_requests` requires an instrument exported as
    `medw_inflight_requests`. If it does not exist, the autoscaler is
    decorative.
    """
    assert f"medw_{metric}" in PY_INSTRUMENTS, (
        f"{chart} scales on medw_{metric}, which no instrument in "
        f"medw_core/metrics.py emits. Defined: {sorted(PY_INSTRUMENTS)}"
    )


def test_inflight_gauge_exports_under_the_name_keda_queries():
    """End to end through the real exporter, not by string manipulation.

    The dot-to-underscore conversion is done by the exporter, so asserting on
    the Python name would be testing our assumption about OTel rather than
    OTel's behaviour.
    """
    from prometheus_client import REGISTRY, generate_latest

    m.configure_prometheus()
    m.inflight_requests.add(2, {"app": "retrieval"})
    body = generate_latest(REGISTRY).decode()

    assert "medw_inflight_requests" in body
    assert 'app="retrieval"' in body, "the ScaledObject filters on this label"


def test_the_label_matches_the_scaledobject_filter():
    """The query filters `{app="<chart name>"}`. The middleware sets that
    attribute from the service name it is constructed with — if the two
    disagree, the series exists and the query still returns nothing."""
    tpl = (CHARTS / "medw-lib/templates/_autoscaling.yaml").read_text()
    assert 'app="{{ .Values.name }}"' in tpl
    for main in (ROOT / "services").glob("*/app/main.py"):
        src = main.read_text()
        if "InFlightMiddleware" in src:
            assert "service=" in src, f"{main.name} must name the service for the app label"


def test_gauge_returns_to_zero_after_a_request():
    """An UpDownCounter that only goes up is a memory leak with a graph.

    The decrement is in a finally block precisely so a raising handler cannot
    strand a count — otherwise the gauge drifts upward forever and the
    autoscaler scales on a number that never comes down.
    """
    import asyncio

    recorded = []

    class Recorder:
        def add(self, amount, attrs=None):
            recorded.append(amount)

    original, m.inflight_requests = m.inflight_requests, Recorder()
    try:
        async def boom(scope, receive, send):
            raise RuntimeError("handler exploded")

        mw = m.InFlightMiddleware(boom, service="retrieval")
        with pytest.raises(RuntimeError):
            asyncio.run(mw({"type": "http"}, None, None))
    finally:
        m.inflight_requests = original

    assert sum(recorded) == 0, f"gauge leaked: {recorded}"


def test_non_http_scopes_are_not_counted():
    """Lifespan and websocket scopes pass through. Counting a lifespan event as
    an in-flight request would add a permanent +1 per pod, which is a constant
    offset the autoscaler cannot distinguish from real load."""
    import asyncio

    recorded = []

    class Recorder:
        def add(self, amount, attrs=None):
            recorded.append(amount)

    original, m.inflight_requests = m.inflight_requests, Recorder()
    try:
        async def app(scope, receive, send):
            return None

        mw = m.InFlightMiddleware(app, service="retrieval")
        asyncio.run(mw({"type": "lifespan"}, None, None))
    finally:
        m.inflight_requests = original

    assert recorded == [], "lifespan scope must not touch the gauge"


def test_scrapes_and_probes_do_not_count_as_load():
    """Measured on a real cluster: the gauge read 1 on an idle service.

    The middleware incremented for the /metrics request, the handler rendered
    the gauge including that increment, and the scrape recorded its own
    observation. KEDA divides by replica count and compares to the target, so a
    permanent floor of one-per-pod is load the autoscaler cannot distinguish
    from real work — and a metric that never returns to zero can never scale
    back to minReplicas.
    """
    import asyncio

    recorded = []

    class Recorder:
        def add(self, amount, attrs=None):
            recorded.append(amount)

    original, m.inflight_requests = m.inflight_requests, Recorder()
    try:
        async def app(scope, receive, send):
            return None

        mw = m.InFlightMiddleware(app, service="gateway")
        for path in ("/metrics", "/healthz", "/readyz"):
            asyncio.run(mw({"type": "http", "path": path}, None, None))
        assert recorded == [], f"{recorded} — scrape/probe traffic must not count"

        asyncio.run(mw({"type": "http", "path": "/search"}, None, None))
        assert recorded == [1, -1], "real traffic must still be counted"
    finally:
        m.inflight_requests = original


def test_one_provider_exports_same_measurement_to_pull_and_push_readers():
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    class CaptureExporter(MetricExporter):
        def __init__(self):
            super().__init__()
            self.batches = []

        def export(self, metrics_data, timeout_millis=10000, **kwargs):
            self.batches.append(metrics_data)
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10000):
            return True

        def shutdown(self, timeout_millis=30000, **kwargs):
            pass

    exporter = CaptureExporter()
    provider = m.build_provider("synthetic", metric_exporter=exporter)
    try:
        counter = provider.get_meter("proof").create_up_down_counter("medw.proof_inflight")
        counter.add(7, {"app": "synthetic"})
        body, _ = m.render_prometheus()
        assert b'medw_proof_inflight{app="synthetic"' in body
        provider.force_flush()
        points = [point for batch in exporter.batches for rm in batch.resource_metrics
                  for sm in rm.scope_metrics for metric in sm.metrics
                  if metric.name == "medw.proof_inflight" for point in metric.data.data_points]
        assert len(points) == 1 and points[0].value == 7
        assert points[0].attributes == {"app": "synthetic"}
    finally:
        provider.shutdown()


def test_configuration_is_once_per_process_not_once_per_lifespan():
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", """
from medw_core import metrics
from opentelemetry import metrics as api
first = metrics.configure(service_name='proof')
assert metrics.configure(service_name='proof') is first
assert api.get_meter_provider() is first
metrics.inflight_requests.add(3, {'app': 'proof'})
body, _ = metrics.render_prometheus()
assert b'medw_inflight_requests{app="proof"' in body
first.shutdown()
"""], check=True)


@pytest.mark.asyncio
async def test_gauge_returns_to_zero_after_cancellation(monkeypatch):
    import asyncio

    recorded = []
    started = asyncio.Event()

    class Recorder:
        def add(self, amount, attrs=None):
            recorded.append(amount)

    async def waiting(scope, receive, send):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(m, "inflight_requests", Recorder())
    task = asyncio.create_task(m.InFlightMiddleware(waiting, "proof")(
        {"type": "http", "path": "/synthetic"}, None, None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded == [1, -1]


def test_real_azure_exporter_converts_same_measurement_without_network(monkeypatch):
    from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
    from azure.monitor.opentelemetry.exporter.export.metrics._exporter import ExportResult

    monkeypatch.setenv("APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_SDKSTATS_DISABLED", "true")
    exporter = AzureMonitorMetricExporter(
        connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
        disable_offline_storage=True,
    )
    captured = []
    def transmit(envelopes):
        captured.extend(envelopes)
        return ExportResult.SUCCESS
    monkeypatch.setattr(exporter, "_transmit", transmit)
    provider = m.build_provider("azure-synthetic", metric_exporter=exporter)
    try:
        provider.get_meter("proof").create_up_down_counter("medw.azure_proof").add(
            5, {"app": "azure-synthetic"})
        body, _ = m.render_prometheus()
        assert b'medw_azure_proof{app="azure-synthetic"' in body
        provider.force_flush()
        points = [point for envelope in captured for point in envelope.data.base_data.metrics]
        assert len(points) == 1
        assert points[0].name == "medw.azure_proof" and points[0].value == 5
        assert captured[0].data.base_data.properties["app"] == "azure-synthetic"
    finally:
        provider.shutdown()


def test_late_azure_configuration_is_rejected_instead_of_silently_lost():
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", """
from medw_core import metrics
metrics.configure_prometheus()
try:
    metrics.configure('synthetic-connection', 'proof')
except RuntimeError as error:
    assert 'Azure reader' in str(error)
else:
    raise AssertionError('must reject the original two-provider initialization mistake')
"""], check=True)

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

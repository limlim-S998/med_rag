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

# One writer action = one trace. The correlation ID is minted at the gateway
# and forwarded on every internal hop, so App Insights can stitch
# gateway -> retrieval -> reranker -> generation into a single request.

import contextvars
import logging
import uuid

CORRELATION_ID = contextvars.ContextVar("correlation_id", default="-")
HEADER = "x-correlation-id"


def new_id() -> str:
    return uuid.uuid4().hex


class CorrelationFilter(logging.Filter):
    def filter(self, record):
        record.correlation_id = CORRELATION_ID.get()
        return True


def configure_logging(level: str, service: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","lvl":"%(levelname)s","svc":"' + service +
        '","cid":"%(correlation_id)s","msg":"%(message)s"}'
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

# In-cluster you would also attach:
#   from azure.monitor.opentelemetry import configure_azure_monitor
#   configure_azure_monitor(connection_string=...)
# which auto-instruments FastAPI, httpx and the Azure SDKs.

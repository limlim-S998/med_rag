"""Vendor-free correlation identity shared by error models and instrumentation."""

import contextvars
import uuid

CORRELATION_ID = contextvars.ContextVar("correlation_id", default="-")
HEADER = "x-correlation-id"


def new_id() -> str:
    return uuid.uuid4().hex

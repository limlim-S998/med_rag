# The error model.
#
# Failures cross four service boundaries here, and without a shared shape each
# hop invents its own. The gateway then cannot tell "the study does not exist"
# from "Qdrant is down" from "the model refused", so it returns 500 for all
# three and the writer sees the same unhelpful message every time.
#
# Two rules:
#
#   1. Every error carries the correlation ID. An error the user reports and
#      an error in App Insights have to be joinable, and asking a medical
#      writer to describe what they typed is not a diagnostic strategy.
#   2. Retryability is a property of the error, not a guess at the call site.
#      Callers ask `err.retryable`; they do not pattern-match on status codes.
#      A 429 from Azure OpenAI and a 503 from Qdrant are both "wait and try",
#      and only the error knows that.

from medw_core.context import CORRELATION_ID


class MedwError(Exception):
    """Base. Never raised directly - the subclass is the message."""

    status_code: int = 500
    retryable: bool = False
    code: str = "internal_error"

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context
        # Captured at raise time, not at handle time: by the time an exception
        # reaches a handler the context var may have been reset by another task.
        self.correlation_id = CORRELATION_ID.get()

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "correlation_id": self.correlation_id,
            **({"context": self.context} if self.context else {}),
        }


# --- client errors: the request was wrong -------------------------------


class NotFound(MedwError):
    status_code = 404
    code = "not_found"


class Forbidden(MedwError):
    """Authenticated, but not for this study.

    Distinct from unauthenticated on purpose: study access is data, not a
    token claim, so this is a lookup failure rather than an auth failure and
    the remedies are completely different.
    """

    status_code = 403
    code = "forbidden"


class ValidationFailed(MedwError):
    status_code = 422
    code = "validation_failed"


# --- dependency errors: something downstream said no --------------------


class DependencyUnavailable(MedwError):
    """A backing service is unreachable. Retryable, and the readiness probe
    should already be failing - if it is not, the probe is checking the wrong
    thing."""

    status_code = 503
    retryable = True
    code = "dependency_unavailable"


class QuotaExceeded(MedwError):
    """Azure OpenAI TPM. Carries `retry_after` when the response supplied one,
    because honouring the server's number beats guessing at backoff."""

    status_code = 429
    retryable = True
    code = "quota_exceeded"

    def __init__(self, message: str, retry_after: float | None = None, **context):
        super().__init__(message, **context)
        self.retry_after = retry_after


# --- domain errors: the system worked and the answer is no --------------


class VerificationFailed(MedwError):
    """A generated section did not survive verification.

    Not retryable, and that is the entire point. A numeral in the output that
    was not in the parsed table is not a transient fault to paper over - it is
    the failure mode the whole design exists to catch. Retrying would roll the
    dice again on a wrong efficacy number reaching a submission document.
    """

    status_code = 422
    retryable = False
    code = "verification_failed"

    def __init__(self, message: str, layer: str, offending: list[str] | None = None, **context):
        super().__init__(message, layer=layer, offending=offending or [], **context)
        self.layer = layer
        self.offending = offending or []


class EmbeddingVersionMismatch(MedwError):
    """The query embedder is not the one that built the index.

    Deserves its own type because the failure is otherwise silent: mismatched
    vector spaces return plausible-looking results, not an error, and nobody
    notices until recall quietly halves.
    """

    status_code = 500
    code = "embedding_version_mismatch"

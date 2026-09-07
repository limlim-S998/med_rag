# The backoff loop, and the bug it used to have.
#
# `with_backoff` dereferenced `e.response.headers` unconditionally. A
# RateLimitError without a response - which happens on a transport failure -
# raised AttributeError from inside the except block, so the retry loop died
# at exactly the moment quota pressure made it necessary. Nothing catches that
# in normal operation because it only appears when you are being throttled.

import asyncio

import pytest
from openai import RateLimitError

from medw_core.rate_limit import TokenBucket, _retry_after, with_backoff


def _rate_limit_error(headers: dict | None) -> RateLimitError:
    """A RateLimitError shaped like the SDK's, without the SDK's constructor
    requirements. Only `.response.headers` is read."""
    err = RateLimitError.__new__(RateLimitError)
    Exception.__init__(err, "429")
    if headers is None:
        err.response = None
    else:
        err.response = type("R", (), {"headers": headers})()
    return err


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after": "12"}, 12.0),
        ({"retry-after": "0.5"}, 0.5),
        ({}, 0.0),                                    # header absent
        ({"retry-after": ""}, 0.0),                   # header empty
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, 0.0),  # HTTP-date form
        (None, 0.0),                                  # no response at all
    ],
)
def test_retry_after_never_raises(headers, expected):
    assert _retry_after(_rate_limit_error(headers)) == expected


@pytest.mark.asyncio
async def test_backoff_survives_error_without_response():
    """The regression. Before the fix this raised AttributeError instead of
    retrying, and the caller saw a crash rather than a slow success."""
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _rate_limit_error(None)
        return "ok"

    assert await with_backoff(flaky, attempts=5) == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_backoff_reraises_on_final_attempt():
    """Exhausted retries must surface the original error, not swallow it -
    the caller needs to know it was throttled, not that it got None."""

    async def always():
        raise _rate_limit_error({"retry-after": "0"})

    with pytest.raises(RateLimitError):
        await with_backoff(always, attempts=2)


@pytest.mark.asyncio
async def test_token_bucket_paces_requests():
    """The bucket exists so we mostly do not hit the quota at all. Draining it
    must force a wait rather than letting the burst through."""
    bucket = TokenBucket(tokens_per_minute=600)     # 10 tokens/second
    await bucket.take(600)                          # drain

    start = asyncio.get_running_loop().time()
    await bucket.take(5)                            # needs ~0.5s to refill
    assert asyncio.get_running_loop().time() - start >= 0.4

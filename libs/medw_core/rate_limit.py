# Azure OpenAI quota is per-deployment tokens-per-minute. Two protections:
# a local token bucket so you mostly do not hit the limit, and honouring
# Retry-After with jitter when you do.

import asyncio
import random
import time

from openai import RateLimitError


class TokenBucket:
    def __init__(self, tokens_per_minute: int):
        self.capacity = tokens_per_minute
        self.tokens = float(tokens_per_minute)
        self.rate = tokens_per_minute / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, n: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)


async def with_backoff(fn, *, attempts: int = 5):
    for i in range(attempts):
        try:
            return await fn()
        except RateLimitError as e:
            if i == attempts - 1:
                raise
            # Defensive on purpose. A RateLimitError does not always carry a
            # response - it can be raised from a transport failure - and the
            # previous version dereferenced it unconditionally. That turned a
            # retryable 429 into an AttributeError raised from inside the
            # handler, killing the backoff loop at exactly the moment it was
            # needed. The failure only appears under real quota pressure,
            # which is the worst time to find out.
            delay = _retry_after(e) or float(2 ** i)
            await asyncio.sleep(delay + random.uniform(0, 0.5))


def _retry_after(exc: RateLimitError) -> float:
    """Seconds the server asked us to wait, or 0.0 if it did not say.

    Honouring the server's number beats guessing: Azure OpenAI knows when the
    per-deployment TPM window rolls over and we do not.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0.0
    try:
        return float(headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        # Retry-After may be an HTTP-date rather than a delta-seconds integer.
        # Falling back to exponential backoff is correct; crashing is not.
        return 0.0

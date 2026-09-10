"""Bounded dependency readiness, independent from process liveness.

Checks perform cheap reads or connection checks, not model inference. Results
are briefly cached so Kubernetes probe frequency cannot amplify an outage.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class DependencyUnavailable(RuntimeError):
    pass


class HealthMonitor:
    def __init__(self, *, timeout: float = 3.0, cache_seconds: float = 5.0):
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.checks: dict[str, Callable[[], Awaitable[object]]] = {}
        self._expires = 0.0
        self._failures: tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    def add(self, name: str, check: Callable[[], Awaitable[object]]) -> None:
        self.checks[name] = check
        self._expires = 0.0

    async def _one(self, name: str, check: Callable[[], Awaitable[object]]) -> str | None:
        try:
            async with asyncio.timeout(self.timeout):
                await check()
            return None
        except Exception:  # noqa: BLE001 - any dependency failure makes the probe unready
            # The response names the dependency, never credentials, URLs or
            # raw database errors. Full exceptions belong in controlled logs.
            return name

    async def check(self) -> None:
        async with self._lock:
            if time.monotonic() >= self._expires:
                failures = await asyncio.gather(
                    *(self._one(name, check) for name, check in self.checks.items())
                )
                self._failures = tuple(f for f in failures if f is not None)
                self._expires = time.monotonic() + self.cache_seconds
        if self._failures:
            raise DependencyUnavailable("unavailable: " + ", ".join(self._failures))


def http_check(client, url: str):
    async def check() -> None:
        response = await client.get(url)
        response.raise_for_status()
    return check


def unavailable_check(reason: str):
    async def check() -> None:
        raise DependencyUnavailable(reason)
    return check

"""Protecting the service from being overwhelmed or abused.

Four layers, applied before any work is done:

- Rate limits per client: questions (which can call the language model and make dozens of
  registry requests) are limited per minute and per hour, and every other request per
  minute. A ceiling on questions from all clients together bounds the total load.
- A cap on answers in progress, so a burst cannot queue up minutes of registry work.
- A limit on request size, enforced while the body is read, so a large body is refused
  before it is held in memory.
- Security headers on every response, including a content security policy for the pages.

The limits live in memory, which is right for a single process, as the deployed service
runs. Several processes would each keep their own counts and would need a shared store.
The client is identified by its address. Behind a proxy, the address the proxy appended to
`X-Forwarded-For` is used, since a client can put anything it likes before it.
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceBusy(Exception):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Window:
    limit: int
    seconds: float
    name: str
    """How the window reads in a message, such as "per minute"."""


@dataclass(frozen=True)
class Quota:
    """The tightest window's standing for one client, for the RateLimit headers."""

    limit: int
    remaining: int
    reset_seconds: int


class SlidingWindowLimiter:
    """Counts each key's requests over one or more trailing windows."""

    def __init__(
        self,
        windows: list[Window],
        *,
        what: str,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 50_000,
    ) -> None:
        self._windows = windows
        self._what = what
        self._clock = clock
        self._max_keys = max_keys
        # Kept in order of last use, so the key idle longest is always first.
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                # Forget the key idle longest, so a flood of addresses cannot exhaust memory.
                self._hits.popitem(last=False)
            hits = self._hits[key] = deque()
        else:
            self._hits.move_to_end(key)
        longest = max(window.seconds for window in self._windows)
        while hits and hits[0] <= now - longest:
            hits.popleft()
        return hits

    def peek(self, key: str) -> Quota:
        now = self._clock()
        return self._quota(self._recent(key, now), now)

    def hit(self, key: str) -> Quota:
        """Count one request for `key`, or raise RateLimited if any window is full."""
        now = self._clock()
        hits = self._recent(key, now)
        for window in self._windows:
            inside = [t for t in hits if t > now - window.seconds]
            if len(inside) >= window.limit:
                retry = max(1, math.ceil(inside[0] + window.seconds - now))
                raise RateLimited(
                    f"Too many {self._what}: the limit is {window.limit} {window.name}. "
                    f"Try again in {retry} seconds.",
                    retry,
                )
        hits.append(now)
        return self._quota(hits, now)

    def _quota(self, hits: deque[float], now: float) -> Quota:
        standings = []
        for window in self._windows:
            inside = [t for t in hits if t > now - window.seconds]
            reset = math.ceil(inside[0] + window.seconds - now) if inside else 0
            standings.append(Quota(window.limit, max(0, window.limit - len(inside)), reset))
        return min(standings, key=lambda q: q.remaining)


BUSY_RETRY_SECONDS = 10
"""What a refusal for being busy tells a client to wait; answers take seconds to a minute."""


class AnswerSlots:
    """A fixed number of answers in progress. A request that finds none free is refused at
    once rather than queued, so a client is never left waiting behind minutes of work."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._busy = 0

    @property
    def free(self) -> int:
        return self.size - self._busy

    def ensure_free(self) -> None:
        """Refuse at once when every slot is taken, for a caller that must answer before
        it can take one (a stream sends its status line first)."""
        if self._busy >= self.size:
            raise ServiceBusy(
                f"The service is already answering {self.size} questions. Try again shortly.",
                retry_after=BUSY_RETRY_SECONDS,
            )

    @asynccontextmanager
    async def take(self) -> AsyncIterator[None]:
        self.ensure_free()
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1


def client_address(request: Request, trusted_proxy_hops: int) -> str:
    """The client's address. With proxies in front, the entry the nearest trusted proxy
    added to X-Forwarded-For, counted from the right, since entries further left are
    whatever the client sent."""
    direct = request.client.host if request.client else "unknown"
    if trusted_proxy_hops <= 0:
        return direct
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    forwarded = [part for part in forwarded if part]
    if len(forwarded) < trusted_proxy_hops:
        return direct
    return forwarded[-trusted_proxy_hops]


def error_envelope(
    code: str, message: str, details: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """The one shape of every error response, built in one place."""
    return {"error": {"code": code, "message": message, "details": details or []}}


def _error_body(code: str, message: str) -> bytes:
    return json.dumps(error_envelope(code, message)).encode()


class BodySizeLimit:
    """Refuses a request body larger than `max_bytes` with a 413, checking the declared
    length first and counting the bytes as they arrive, so the body is never held whole."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(send)
            return
        received = 0
        started = refused = False

        async def counting_receive() -> Message:
            # Once the limit is crossed, the 413 is sent at once and the application is told
            # the client has gone, so it stops reading. Raising here instead would surface as
            # the framework's own "could not parse the body" error.
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not started:
                    refused = True
                    await self._refuse(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if refused:
                return  # the 413 already went out
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _refuse(self, send: Send) -> None:
        body = _error_body(
            "request_too_large", f"The request body is larger than {self.max_bytes} bytes."
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# The pages load their chart libraries and fonts from these origins only. Vega and Plotly
# compile chart expressions at run time, so scripts need 'unsafe-eval', and the pages carry
# one small inline script each.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net",
        "img-src 'self' data: blob:",
        "connect-src 'self' https://cdn.jsdelivr.net",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)


class SecurityHeaders:
    """Adds security headers to every response, and the RateLimit headers when a limited
    endpoint recorded the client's standing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
                ]
                if _is_https(scope):
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                quota: Any = scope.get("state", {}).get("quota")
                if isinstance(quota, Quota):
                    extra += [
                        (b"ratelimit-limit", str(quota.limit).encode()),
                        (b"ratelimit-remaining", str(quota.remaining).encode()),
                        (b"ratelimit-reset", str(quota.reset_seconds).encode()),
                    ]
                headers += [(name, value) for name, value in extra if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


def _is_https(scope: Scope) -> bool:
    forwarded = dict(scope.get("headers", [])).get(b"x-forwarded-proto", b"")
    return scope.get("scheme") == "https" or forwarded.split(b",")[0].strip() == b"https"

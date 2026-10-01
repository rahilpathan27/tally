"""Admission control: cap in-flight work per process and shed the rest quickly.

Without a cap, an overloaded service queues every request; latency grows without bound and
clients time out after the server has done the work anyway (the Phase 15 spike test measured a
16 s p99). With a cap, requests beyond it get an immediate ``503 OVERLOADED`` with
``Retry-After``. That is safe for merchants because mutations carry idempotency keys and the
gateway releases a key when the response is 5xx, so the retry runs exactly once.

Only matching path prefixes are limited; health checks and metrics are never shed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from libs.observability.metrics import LOAD_SHED

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_BODY = json.dumps(
    {"detail": {"code": "OVERLOADED", "message": "Service is busy; retry shortly."}}
).encode()


class AdmissionControl:
    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        max_in_flight: int,
        prefixes: tuple[str, ...] = ("/v1/",),
        retry_after_seconds: int = 1,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        self.app = app
        self.service = service
        self.max_in_flight = max_in_flight
        self.prefixes = prefixes
        self.retry_after = str(retry_after_seconds).encode()
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        if self.in_flight >= self.max_in_flight:
            LOAD_SHED.labels(self.service).inc()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", self.retry_after),
                        (b"content-length", str(len(_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _BODY})
            return
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1

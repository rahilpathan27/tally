"""HTTP hardening shared by every FastAPI service.

* security headers on every response (HSTS, no sniffing, frame denial, strict CSP for JSON APIs,
  no referrer, no caching of API responses);
* a request body size limit enforced while reading, so a large body is rejected before it is
  buffered;
* JSON-only mutations: POST/PUT/PATCH bodies must be ``application/json`` unless the route is
  explicitly allowed another type (statement uploads).
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cache-control", b"no-store"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)


class SecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = 1_048_576,
        raw_body_paths: Iterable[str] = (),
        large_body_paths: Iterable[str] = (),
        large_body_limit: int = 512 * 1024 * 1024,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.raw_body_paths = tuple(raw_body_paths)
        self.large_body_paths = tuple(large_body_paths)
        self.large_body_limit = large_body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        limit = (
            self.large_body_limit if path.startswith(self.large_body_paths) else self.max_body_bytes
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in SECURITY_HEADERS if k not in existing
                ]
            await send(message)

        async def reject(status: int, code: bytes) -> None:
            body = b'{"detail":{"code":"' + code + b'","message":"Request rejected."}}'
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await reject(413, b"REQUEST_TOO_LARGE")
                    return
            except ValueError:
                await reject(400, b"INVALID_CONTENT_LENGTH")
                return
        if method in {"POST", "PUT", "PATCH"} and not path.startswith(self.raw_body_paths):
            content_type = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
            has_body = declared not in (None, b"0") or b"transfer-encoding" in headers
            if has_body and content_type != b"application/json":
                await reject(415, b"UNSUPPORTED_MEDIA_TYPE")
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            await reject(413, b"REQUEST_TOO_LARGE")


class _BodyTooLarge(Exception):
    pass

"""FastAPI route class that composes gateway authentication, rate limiting, and retries."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.routing import APIRoute
from libs.idempotency.store import (
    IdempotencyPayloadConflict,
    PostgresIdempotencyStore,
    request_fingerprint,
)
from libs.observability.metrics import IDEMPOTENT_REPLAYS, MERCHANT_REQUESTS, RATE_LIMITED
from libs.security.rate_limit import MerchantRateLimiter
from starlette.responses import JSONResponse, Response

from services.api_gateway.auth import MerchantHmacAuth, MerchantPrincipal, require_scope

_SCOPE_ATTRIBUTE = "__tally_required_scope__"


def requires_scope(scope: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if not scope:
        raise ValueError("scope is required")

    def decorate(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, _SCOPE_ATTRIBUTE, scope)
        return endpoint

    return decorate


class GatewayRoute(APIRoute):
    """Wrap merchant routes; install auth, limiter, and store on ``app.state``."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original_handler = super().get_route_handler()
        required_scope = getattr(self.endpoint, "__tally_required_scope__", None)

        async def gateway_handler(request: Request) -> Response:
            app_state = request.app.state
            auth: MerchantHmacAuth = app_state.gateway_auth
            limiter: MerchantRateLimiter = app_state.gateway_rate_limiter
            idempotency: PostgresIdempotencyStore = app_state.gateway_idempotency
            principal: MerchantPrincipal = await auth(request)
            request.state.merchant_principal = principal
            if required_scope is not None:
                require_scope(principal, str(required_scope))

            limit, window_seconds = app_state.gateway_rate_limit_policy(principal)
            rate = await limiter.check(
                principal.merchant_id,
                limit=limit,
                window_seconds=window_seconds,
            )
            if not rate.allowed:
                RATE_LIMITED.inc()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"code": "RATE_LIMITED", "message": "Request rate limit exceeded."},
                    headers={"Retry-After": str(rate.reset_after_seconds)},
                )

            is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
            idempotency_key: str | None = None
            fingerprint: bytes | None = None
            if is_mutation:
                idempotency_key = request.headers.get("idempotency-key", "")
                if not 1 <= len(idempotency_key) <= 200:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REQUIRED",
                            "message": "Mutating requests require an Idempotency-Key header.",
                        },
                    )
                query = request.scope.get("query_string", b"").decode("latin-1")
                raw_path = request.scope.get("raw_path", request.url.path.encode())
                path = raw_path.decode("latin-1") + (f"?{query}" if query else "")
                request_body = await request.body()
                fingerprint = request_fingerprint(request.method, path, request_body)
                try:
                    reservation = await idempotency.begin(
                        principal.merchant_id, idempotency_key, fingerprint
                    )
                except IdempotencyPayloadConflict as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": "Idempotency key was used with a different request.",
                        },
                    ) from exc
                if reservation.outcome == "replay":
                    IDEMPOTENT_REPLAYS.inc()
                    assert reservation.response_status is not None
                    assert reservation.response_body is not None
                    return JSONResponse(
                        status_code=reservation.response_status,
                        content=reservation.response_body,
                    )
                if reservation.outcome == "in_progress":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_IN_PROGRESS",
                            "message": "A request with this idempotency key is still in progress.",
                        },
                        headers={"Retry-After": "1"},
                    )

            try:
                response = await original_handler(request)
                MERCHANT_REQUESTS.labels(
                    principal.merchant_id, f"{response.status_code // 100}xx"
                ).inc()
            except HTTPException as exc:
                MERCHANT_REQUESTS.labels(principal.merchant_id, f"{exc.status_code // 100}xx").inc()
                if not is_mutation:
                    raise
                assert idempotency_key is not None and fingerprint is not None
                if exc.status_code >= 500:
                    await idempotency.release(principal.merchant_id, idempotency_key, fingerprint)
                else:
                    # Client errors are deterministic for this payload; replay them verbatim.
                    await idempotency.complete(
                        principal.merchant_id,
                        idempotency_key,
                        fingerprint,
                        exc.status_code,
                        {"detail": exc.detail},
                    )
                raise
            except Exception:
                if is_mutation:
                    assert idempotency_key is not None and fingerprint is not None
                    await idempotency.release(principal.merchant_id, idempotency_key, fingerprint)
                raise
            if is_mutation:
                assert idempotency_key is not None and fingerprint is not None
                response_bytes = getattr(response, "body", None)
                if response.media_type != "application/json" or response_bytes is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={
                            "code": "IDEMPOTENCY_RESPONSE_UNSUPPORTED",
                            "message": (
                                "Mutating gateway routes must return a JSON object or array."
                            ),
                        },
                    )
                response_body = json.loads(response_bytes)
                if not isinstance(response_body, (dict, list)):
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={
                            "code": "IDEMPOTENCY_RESPONSE_UNSUPPORTED",
                            "message": (
                                "Mutating gateway routes must return a JSON object or array."
                            ),
                        },
                    )
                await idempotency.complete(
                    principal.merchant_id,
                    idempotency_key,
                    fingerprint,
                    response.status_code,
                    response_body,
                )
            return response

        return gateway_handler

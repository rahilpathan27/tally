"""FastAPI dependency for tenant-scoped, nonce-protected HMAC requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException, Request, status
from libs.security.hmac_auth import AuthenticationError, SignedRequest, verify_request


@dataclass(frozen=True, slots=True)
class MerchantPrincipal:
    merchant_id: str
    key_id: str
    scopes: frozenset[str]
    mode: str


SecretDecryptor = Callable[[str, bytes], bytes]


class MerchantHmacAuth:
    """Resolve active keys, verify signatures, then atomically consume each nonce."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        decrypt_secret: SecretDecryptor,
        *,
        tolerance_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if tolerance_seconds <= 0:
            raise ValueError("tolerance_seconds must be positive")
        self._pool = pool
        self._decrypt_secret = decrypt_secret
        self._tolerance_seconds = tolerance_seconds
        self._clock = clock

    async def __call__(self, request: Request) -> MerchantPrincipal:
        key_id = request.headers.get("x-tally-key-id", "")
        nonce = request.headers.get("x-tally-nonce", "")
        raw_timestamp = request.headers.get("x-tally-timestamp", "")
        signature = request.headers.get("x-tally-signature", "")
        try:
            timestamp = int(raw_timestamp)
        except ValueError as exc:
            raise self._unauthorized() from exc
        if not key_id or not 16 <= len(nonce) <= 200 or not signature:
            raise self._unauthorized()

        query = request.scope.get("query_string", b"").decode("latin-1")
        raw_path = request.scope.get("raw_path", request.url.path.encode())
        path = raw_path.decode("latin-1") + (f"?{query}" if query else "")
        body = await request.body()

        async with self._pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT * FROM gateway_lookup_api_key($1)",
                key_id,
            )
            if record is None:
                raise self._unauthorized()
            try:
                secret = self._decrypt_secret(key_id, bytes(record["secret_ciphertext"]))
                verify_request(
                    SignedRequest(key_id, timestamp, nonce, signature),
                    method=request.method,
                    path=path,
                    body=body,
                    secret_lookup=lambda requested_key: secret if requested_key == key_id else None,
                    now=int(self._clock().timestamp()),
                    tolerance_seconds=self._tolerance_seconds,
                )
            except (AuthenticationError, ValueError) as exc:
                raise self._unauthorized() from exc
            except InvalidTag as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "AUTH_KEY_UNAVAILABLE",
                        "message": "Authentication is unavailable.",
                    },
                ) from exc

            consumed = await connection.fetchval(
                "SELECT gateway_consume_nonce($1, $2, $3)",
                key_id,
                nonce,
                self._clock() + timedelta(seconds=2 * self._tolerance_seconds),
            )
            if not consumed:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "REQUEST_REPLAYED",
                        "message": "Request nonce was already used.",
                    },
                )

        return MerchantPrincipal(
            merchant_id=str(record["merchant_id"]),
            key_id=key_id,
            scopes=frozenset(record["scopes"]),
            mode=str(record["mode"]),
        )

    @staticmethod
    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_AUTHENTICATION", "message": "Request authentication failed."},
            headers={"WWW-Authenticate": "Tally-HMAC"},
        )


async def set_merchant_rls_context(connection: asyncpg.Connection, merchant_id: str) -> None:
    """Bind tenant scope transaction-locally before querying RLS-protected data."""
    await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)


def require_scope(principal: MerchantPrincipal, required_scope: str) -> None:
    if required_scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "INSUFFICIENT_SCOPE", "message": "API key lacks the required scope."},
        )

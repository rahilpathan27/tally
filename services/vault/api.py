"""Isolated tokenization API; only the network-simulator credential can detokenize."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from libs.observability.metrics import instrument
from libs.observability.tracing import configure_tracing
from libs.security.envelope_encryption import EnvelopeCipher
from libs.security.http import SecurityMiddleware
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from services.vault.model import PUBLISHED_TEST_PANS, TokenMetadata, validate_test_card
from services.vault.repository import VaultRepository


@dataclass(frozen=True, slots=True)
class VaultConfig:
    database_url: str
    key_encryption_key: bytes
    allowed_test_pans: frozenset[str]
    tokenize_client_key: str
    network_simulator_key: str
    # Publishable keys identify a checkout page; they are not secrets (like Stripe's pk_*).
    publishable_keys: frozenset[str] = frozenset()

    @classmethod
    def from_environment(cls) -> VaultConfig:
        database_url = os.environ.get("VAULT_DATABASE_URL")
        encoded_kek = os.environ.get("TALLY_VAULT_KEK_B64")
        encoded_pans = os.environ.get("TALLY_VAULT_TEST_PANS", "")
        tokenize_key = os.environ.get("TALLY_VAULT_TOKENIZE_KEY")
        network_key = os.environ.get("TALLY_VAULT_NETWORK_KEY")
        if not database_url or not encoded_kek or not tokenize_key or not network_key:
            raise RuntimeError("vault database URL, KEK, and service keys must be configured")
        try:
            kek = base64.b64decode(encoded_kek, altchars=b"-_", validate=True)
        except ValueError as exc:
            raise RuntimeError("vault KEK must be base64-encoded") from exc
        allowed = frozenset(value.strip() for value in encoded_pans.split(",") if value.strip())
        if not allowed:
            raise RuntimeError("TALLY_VAULT_TEST_PANS must contain published test PANs")
        if not allowed <= PUBLISHED_TEST_PANS:
            raise RuntimeError(
                "TALLY_VAULT_TEST_PANS contains a PAN outside the published test set"
            )
        publishable = frozenset(
            key.strip()
            for key in os.environ.get("TALLY_VAULT_PUBLISHABLE_KEYS", "").split(",")
            if key.strip()
        )
        return cls(database_url, kek, allowed, tokenize_key, network_key, publishable)


class TokenizeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    pan: SecretStr
    expiry_month: Annotated[int, Field(gt=0, le=12)]
    expiry_year: Annotated[int, Field(ge=2000, le=9999)]


class TokenizeResponse(BaseModel):
    payment_method_token: str
    last4: str
    expiry_month: int
    expiry_year: int


class DetokenizeResponse(BaseModel):
    pan: str


def _repository(request: Request) -> VaultRepository:
    repository = getattr(request.app.state, "vault_repository", None)
    if repository is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "vault is not ready")
    return cast(VaultRepository, repository)


def _config(request: Request) -> VaultConfig:
    config = getattr(request.app.state, "vault_config", None)
    if config is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "vault is not ready")
    return cast(VaultConfig, config)


def _authorize(provided: str | None, expected: str, scheme: str) -> None:
    if provided is None or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "VAULT_AUTH_FAILED", "message": "Vault authentication failed."},
            headers={"WWW-Authenticate": scheme},
        )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = VaultConfig.from_environment()

        async def set_runtime_role(connection: asyncpg.Connection) -> None:
            await connection.execute("SET ROLE tally_vault_app")

        pool = await asyncpg.create_pool(
            config.database_url, min_size=1, max_size=10, setup=set_runtime_role
        )
        app.state.vault_config = config
        app.state.vault_repository = VaultRepository(
            pool, EnvelopeCipher(config.key_encryption_key)
        )
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Tally Vault", version="1.0.0", lifespan=lifespan)
    app.add_middleware(SecurityMiddleware, max_body_bytes=16_000)
    instrument(app, "vault")
    configure_tracing("vault", app)
    origins = [o for o in os.environ.get("TALLY_VAULT_CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["POST"],
            allow_headers=["content-type", "x-publishable-key"],
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": {"code": "INVALID_VAULT_REQUEST", "message": "Vault request is invalid."}
            },
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> dict[str, str]:
        try:
            await _repository(request).health()
        except (asyncpg.PostgresError, OSError) as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "vault database is not ready"
            ) from exc
        return {"status": "ready"}

    @app.post("/v1/tokens", response_model=TokenizeResponse, status_code=status.HTTP_201_CREATED)
    async def tokenize(
        body: TokenizeRequest,
        request: Request,
        x_vault_client_key: Annotated[str | None, Header()] = None,
    ) -> TokenizeResponse:
        config = _config(request)
        _authorize(x_vault_client_key, config.tokenize_client_key, "Vault-Client")
        return await _tokenize(body, request, config)

    @app.post(
        "/public/v1/tokens", response_model=TokenizeResponse, status_code=status.HTTP_201_CREATED
    )
    async def tokenize_from_checkout(
        body: TokenizeRequest,
        request: Request,
        x_publishable_key: Annotated[str | None, Header()] = None,
    ) -> TokenizeResponse:
        """Browser checkout posts the card straight here, so the PAN never reaches the merchant."""
        config = _config(request)
        if x_publishable_key is None or x_publishable_key not in config.publishable_keys:
            _authorize(None, "", "Vault-Publishable")
        return await _tokenize(body, request, config)

    async def _tokenize(
        body: TokenizeRequest, request: Request, config: VaultConfig
    ) -> TokenizeResponse:
        pan = body.pan.get_secret_value()
        try:
            validate_test_card(
                pan,
                body.expiry_month,
                body.expiry_year,
                allowed_test_pans=config.allowed_test_pans,
                today=datetime.now(UTC).date(),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "INVALID_TEST_CARD", "message": str(exc)},
            ) from exc
        token: TokenMetadata = await _repository(request).tokenize(
            pan, body.expiry_month, body.expiry_year
        )
        return TokenizeResponse(
            payment_method_token=token.token,
            last4=token.last4,
            expiry_month=token.expiry_month,
            expiry_year=token.expiry_year,
        )

    @app.post(
        "/internal/v1/tokens/{token}/detokenize",
        response_model=DetokenizeResponse,
        include_in_schema=False,
    )
    async def detokenize(
        token: Annotated[str, Path(min_length=8, max_length=200)],
        request: Request,
        x_vault_network_key: Annotated[str | None, Header()] = None,
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> DetokenizeResponse:
        config = _config(request)
        _authorize(x_vault_network_key, config.network_simulator_key, "Vault-Network")
        request_id = x_request_id or str(uuid4())
        if len(request_id) > 200:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "request ID is too long")
        audit_request_id = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            pan = await _repository(request).detokenize(
                token, "network-simulator", audit_request_id
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "TOKEN_NOT_FOUND",
                    "message": "Payment method token was not found.",
                },
            ) from exc
        return DetokenizeResponse(pan=pan)

    return app


app = create_app()

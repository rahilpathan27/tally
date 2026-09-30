"""Back-office API (BFF) for the merchant dashboard and the ops/risk console."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from libs.security.http import SecurityMiddleware
from libs.security.jwt_tokens import KeyRing
from libs.security.key_encryption import ApiKeyCipher

from services.backoffice_api import auth, routes_merchant, routes_ops
from services.backoffice_api.approvals import ApprovalError


def configure(
    app: FastAPI,
    *,
    pool: asyncpg.Pool,
    secret_cipher: ApiKeyCipher,
    api_key_cipher: ApiKeyCipher,
    key_ring: KeyRing,
    clients: dict[str, tuple[httpx.AsyncClient, str]],
    secure_cookies: bool = True,
    chaos_enabled: bool = False,
    simulator_admin_key: str = "",
    webhook_trusted_hosts: tuple[str, ...] = (),
) -> None:
    state = app.state
    state.pool = pool
    state.secret_cipher = secret_cipher
    state.api_key_cipher = api_key_cipher
    state.webhook_cipher = api_key_cipher
    state.webhook_trusted_hosts = webhook_trusted_hosts
    state.key_ring = key_ring
    state.secure_cookies = secure_cookies
    state.chaos_enabled = chaos_enabled
    state.simulator_admin_key = simulator_admin_key
    for name, (client, key) in clients.items():
        setattr(state, f"{name}_http", client)
        setattr(state, f"{name}_key", key)


def create_app(allowed_origins: tuple[str, ...] = ()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "pool", None) is not None:
            yield
            return
        pool = await asyncpg.create_pool(os.environ["TALLY_DATABASE_URL"], min_size=2, max_size=20)
        internal = os.environ.get("TALLY_INTERNAL_KEY", "")
        clients: dict[str, tuple[httpx.AsyncClient, str]] = {}
        for name, variable, default, key in (
            (
                "core",
                "TALLY_CORE_URL",
                "http://127.0.0.1:8000",
                os.environ.get("TALLY_RECOVERY_KEY", ""),
            ),
            ("ledger", "TALLY_LEDGER_URL", "http://127.0.0.1:8001", ""),
            ("recon", "TALLY_RECON_URL", "http://127.0.0.1:8020", internal),
            ("risk", "TALLY_RISK_URL", "http://127.0.0.1:8030", internal),
            ("sim_bank", "TALLY_BANK_SIM_URL", "http://127.0.0.1:8010", ""),
            ("sim_network", "TALLY_CARD_NETWORK_URL", "http://127.0.0.1:8012", ""),
            ("sim_psp", "TALLY_PAYER_PSP_URL", "http://127.0.0.1:8011", ""),
        ):
            clients[name] = (
                httpx.AsyncClient(base_url=os.environ.get(variable, default), timeout=15),
                key,
            )
        master = base64.b64decode(os.environ["TALLY_API_KEY_ENCRYPTION_KEY"], altchars=b"-_")
        configure(
            app,
            pool=pool,
            secret_cipher=ApiKeyCipher(
                base64.b64decode(os.environ["TALLY_BACKOFFICE_SECRET_KEY"], altchars=b"-_")
            ),
            api_key_cipher=ApiKeyCipher(master),
            key_ring=KeyRing.generate(),
            clients=clients,
            secure_cookies=os.environ.get("TALLY_INSECURE_COOKIES") != "1",
            chaos_enabled=os.environ.get("TALLY_CHAOS_CONTROL") == "1",
            simulator_admin_key=os.environ.get("TALLY_SIMULATOR_ADMIN_KEY", ""),
        )
        try:
            yield
        finally:
            for client, _ in clients.values():
                await client.aclose()
            await pool.close()

    app = FastAPI(title="Tally Back Office", version="1.0.0", lifespan=lifespan)
    app.state.pool = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "x-csrf-token"],
    )
    app.add_middleware(SecurityMiddleware, max_body_bytes=2_000_000)

    @app.exception_handler(ApprovalError)
    async def approval_error(request: Request, exc: ApprovalError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=422, content={"detail": {"code": "INVALID_REQUEST", "message": "Invalid."}}
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, Any]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(routes_merchant.router)
    app.include_router(routes_ops.router)
    return app


app = create_app(
    tuple(
        o for o in os.environ.get("TALLY_CONSOLE_ORIGINS", "http://localhost:3000").split(",") if o
    )
)

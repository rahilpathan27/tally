"""In-process Tally stack wired over ASGI transports against real PostgreSQL and Redis.

Every service (core, ledger, vault, bank/PSP/network simulators) runs as its real FastAPI
application; only the HTTP hop is replaced by ``httpx.ASGITransport``. ``FlakyTransport``
wraps a hop so the harness can drop a request before it reaches the service or lose the
response after the service has committed its effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import asyncpg
import httpx
from libs.idempotency.store import PostgresIdempotencyStore
from libs.security.envelope_encryption import EnvelopeCipher
from libs.security.hmac_auth import sign_request
from libs.security.key_encryption import ApiKeyCipher
from libs.security.rate_limit import MerchantRateLimiter
from redis.asyncio import Redis
from scripts.migrate import migrate
from services.api_gateway.auth import MerchantHmacAuth
from services.core.api import create_app as create_core_app
from services.core.money_routes import configure_money_services
from services.core.recovery import CircuitBreaker
from services.core.repository import PaymentIntentRepository
from services.simulators.bank.api import BankSimulatorConfig
from services.simulators.bank.api import create_app as create_bank_app
from services.simulators.network.api import CardNetworkConfig
from services.simulators.network.api import create_app as create_network_app
from services.simulators.payer_psp.api import PayerPspConfig
from services.simulators.payer_psp.api import create_app as create_psp_app
from services.vault.api import VaultConfig
from services.vault.api import create_app as create_vault_app
from services.vault.repository import VaultRepository

LOCAL_HOST = "postgresql://tally:tally-local-only@127.0.0.1"
GENERAL_ADMIN = f"{LOCAL_HOST}:55432/tally"
LEDGER_ADMIN = f"{LOCAL_HOST}:55433/tally_ledger_v1"
VAULT_ADMIN = f"{LOCAL_HOST}:55434/tally_vault"
TEST_PAN = "4242424242424242"
PROVISIONING_KEY = "stack-ledger-provisioner"
SIMULATOR_KEY = "stack-network-simulator-key"
TOKENIZE_KEY = "stack-tokenize-client-key"
VAULT_NETWORK_KEY = "stack-vault-network-key"
MERCHANT_SECRET = b"stack-merchant-hmac-secret-with-more-than-32-bytes"

FaultMode = Literal["drop_request", "lose_response"]


class TransportFault(httpx.TransportError):
    """Injected network failure."""


@dataclass(slots=True)
class FlakyTransport(httpx.AsyncBaseTransport):
    inner: httpx.AsyncBaseTransport
    faults: list[FaultMode] = field(default_factory=list)
    calls: int = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "drop_request":
            raise TransportFault("injected: request dropped before delivery")
        response = await self.inner.handle_async_request(request)
        if fault == "lose_response":
            await response.aread()
            raise TransportFault("injected: response lost after delivery")
        return response


async def recreate_database(admin_url: str, name: str) -> str:
    connection = await asyncpg.connect(admin_url)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return admin_url.rsplit("/", 1)[0] + f"/{name}"


async def provision_databases(prefix: str) -> tuple[str, str, str]:
    """Create empty general, ledger and vault databases and apply every migration."""
    general = await recreate_database(GENERAL_ADMIN, f"{prefix}_general")
    ledger = await recreate_database(LEDGER_ADMIN, f"{prefix}_ledger")
    vault = await recreate_database(VAULT_ADMIN, f"{prefix}_vault")
    for service in ("gateway", "core", "recon", "risk", "backoffice"):
        await migrate(service, general)
    await migrate("ledger", ledger)
    await migrate("vault", vault)
    ledger_conn = await asyncpg.connect(ledger)
    general_conn = await asyncpg.connect(general)
    try:
        await ledger_conn.execute(open("services/ledger/seeds/001_local_chart.sql").read())
        await general_conn.execute(open("services/core/seeds/001_local_vpas.sql").read())
    finally:
        await ledger_conn.close()
        await general_conn.close()
    return general, ledger, vault


@dataclass(slots=True)
class LocalStack:
    general_pool: asyncpg.Pool
    ledger_pool: asyncpg.Pool
    vault_pool: asyncpg.Pool
    redis: Redis
    core_app: object
    bank_app: object
    psp_app: object
    network_app: object
    vault_app: object
    risk_app: object | None
    client: httpx.AsyncClient
    ledger_transport: FlakyTransport
    bank_transport: FlakyTransport
    network_transport: FlakyTransport
    merchant_id: str
    key_id: str
    card_token: str
    clients: list[httpx.AsyncClient]

    @property
    def repository(self) -> PaymentIntentRepository:
        return PaymentIntentRepository(self.general_pool)

    async def send(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        nonce = f"stack-{uuid4().hex}"
        timestamp = int(datetime.now(UTC).timestamp())
        headers = {
            "x-tally-key-id": self.key_id,
            "x-tally-timestamp": str(timestamp),
            "x-tally-nonce": nonce,
            "x-tally-signature": sign_request(MERCHANT_SECRET, method, path, raw, timestamp, nonce),
        }
        if raw:
            headers["content-type"] = "application/json"
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        return await self.client.request(method, path, content=raw, headers=headers)

    async def close(self) -> None:
        for client in self.clients:
            await client.aclose()
        await self.redis.aclose()
        await self.general_pool.close()
        await self.ledger_pool.close()
        await self.vault_pool.close()


async def build_stack(
    general_url: str,
    ledger_url: str,
    vault_url: str,
    redis_url: str,
    *,
    merchant_id: str | None = None,
    rate_limit: int = 1_000_000,
    object_store: object | None = None,
    webhook_http: httpx.AsyncClient | None = None,
    trusted_webhook_hosts: tuple[str, ...] = (),
    risk: bool = False,
    model_dir: str = "ml/artifacts/fraud-gbm-v1",
) -> LocalStack:
    general_pool = await asyncpg.create_pool(general_url, min_size=2, max_size=20)
    ledger_pool = await asyncpg.create_pool(ledger_url, min_size=2, max_size=20)

    async def set_vault_role(connection: asyncpg.Connection) -> None:
        await connection.execute("SET ROLE tally_vault_app")

    vault_pool = await asyncpg.create_pool(vault_url, min_size=1, max_size=5, setup=set_vault_role)
    redis = Redis.from_url(redis_url)
    merchant_id = merchant_id or f"stack-{uuid4().hex[:12]}"
    key_id = f"key-{uuid4().hex}"
    cipher = ApiKeyCipher(b"s" * 32)

    vault_app = create_vault_app()
    vault_app.state.vault_config = VaultConfig(
        vault_url, b"v" * 32, frozenset({TEST_PAN}), TOKENIZE_KEY, VAULT_NETWORK_KEY
    )
    vault_app.state.vault_repository = VaultRepository(vault_pool, EnvelopeCipher(b"v" * 32))
    vault_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_app), base_url="http://vault"
    )
    network_app = create_network_app(
        CardNetworkConfig("http://vault", VAULT_NETWORK_KEY, SIMULATOR_KEY)
    )
    network_app.state.http_client = vault_http
    bank_app = create_bank_app(BankSimulatorConfig())
    psp_app = create_psp_app(PayerPspConfig())

    from services.ledger.api import app as ledger_app

    ledger_app.state.pool = ledger_pool
    ledger_app.state.provisioning_key = PROVISIONING_KEY

    ledger_transport = FlakyTransport(httpx.ASGITransport(app=ledger_app))
    bank_transport = FlakyTransport(httpx.ASGITransport(app=bank_app))
    network_transport = FlakyTransport(httpx.ASGITransport(app=network_app))

    core_app = create_core_app()
    state = core_app.state
    state.pool = general_pool
    state.gateway_auth = MerchantHmacAuth(general_pool, cipher.decrypt)
    state.gateway_rate_limiter = MerchantRateLimiter(redis, namespace=f"stack:{merchant_id}")
    state.gateway_idempotency = PostgresIdempotencyStore(general_pool)
    state.gateway_rate_limit_policy = lambda _: (rate_limit, 60)
    state.payment_repository = PaymentIntentRepository(general_pool)
    state.bank_breaker = CircuitBreaker()
    state.card_network_breaker = CircuitBreaker()
    state.ledger_provisioning_key = PROVISIONING_KEY
    state.network_simulator_key = SIMULATOR_KEY
    state.recovery_key = "stack-recovery-key"
    ledger_http = httpx.AsyncClient(transport=ledger_transport, base_url="http://ledger")
    network_http = httpx.AsyncClient(transport=network_transport, base_url="http://network")
    bank_http = httpx.AsyncClient(transport=bank_transport, base_url="http://bank")
    psp_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=psp_app), base_url="http://psp")
    state.ledger_http = ledger_http
    state.card_network_http = network_http
    state.bank_http = bank_http
    state.payer_psp_http = psp_http
    core_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=core_app), base_url="http://core", timeout=30
    )
    configure_money_services(
        state,
        pool=general_pool,
        cipher=cipher,
        object_store=object_store,
        webhook_http=webhook_http,
        trusted_webhook_hosts=trusted_webhook_hosts,
    )

    risk_app = None
    risk_client = None
    if risk:
        from pathlib import Path

        from services.risk import registry
        from services.risk.api import create_app as create_risk_app
        from services.risk.engine import RiskEngine
        from services.risk.features import RedisFeatureState
        from services.risk.rules import DEFAULT_RULES

        await registry.bootstrap(general_pool, Path(model_dir), DEFAULT_RULES)

        async def resume(payment_id: str, outcome: str) -> None:
            await core_client.post(
                f"/internal/v1/payments/{payment_id}/risk-resolution",
                json={"outcome": outcome},
                headers={"x-internal-key": state.recovery_key, "x-actor": "risk-analyst"},
            )

        engine = RiskEngine(
            pool=general_pool,
            state=RedisFeatureState(redis, namespace=f"rf:{uuid4().hex[:8]}"),
            step_up_secret=b"stack-step-up-secret",
            on_resolution=resume,
        )
        await engine.refresh(force=True)
        risk_app = create_risk_app(engine, internal_key="stack-risk-key")
        risk_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=risk_app), base_url="http://risk"
        )
        state.risk_http = risk_client
        state.risk_key = "stack-risk-key"
        state.risk_timeout_seconds = 5.0  # in-process tests; the service budget is measured
    await general_pool.execute(
        """INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)
           ON CONFLICT (merchant_id) DO NOTHING""",
        merchant_id,
        "Local stack merchant",
    )
    await general_pool.execute(
        """INSERT INTO merchant_api_keys(
               key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
           ) VALUES ($1, $2, $3, $4, 'test', $5)""",
        key_id,
        merchant_id,
        cipher.encrypt(key_id, MERCHANT_SECRET),
        [
            "payments:write",
            "payments:read",
            "settlements:read",
            "disputes:read",
            "disputes:write",
            "webhooks:read",
            "webhooks:write",
        ],
        datetime.now(UTC) + timedelta(days=1),
    )
    token = (await vault_app.state.vault_repository.tokenize(TEST_PAN, 12, 2099)).token
    return LocalStack(
        general_pool=general_pool,
        ledger_pool=ledger_pool,
        vault_pool=vault_pool,
        redis=redis,
        core_app=core_app,
        bank_app=bank_app,
        psp_app=psp_app,
        network_app=network_app,
        vault_app=vault_app,
        risk_app=risk_app,
        client=core_client,
        ledger_transport=ledger_transport,
        bank_transport=bank_transport,
        network_transport=network_transport,
        merchant_id=merchant_id,
        key_id=key_id,
        card_token=token,
        clients=[
            client
            for client in (
                core_client,
                ledger_http,
                network_http,
                bank_http,
                psp_http,
                vault_http,
                webhook_http,
                risk_client,
            )
            if client is not None
        ],
    )

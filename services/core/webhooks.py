"""Outbox relay (Kafka + webhook fan-out) and SSRF-safe, signed webhook delivery."""

from __future__ import annotations

import fnmatch
import json
import os
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Protocol, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from libs.observability.metrics import SSRF_BLOCKED, WEBHOOK_RESULTS
from libs.security.ssrf import (
    Resolver,
    UnsafeDestination,
    parse_webhook_url,
    system_resolver,
    vetted_address,
)
from libs.security.webhook_signing import HEADER, sign_payload
from pydantic import BaseModel, ConfigDict, Field

MAX_ATTEMPTS = 8
EVENT_PATTERN = r"^(\*|[a-z_]+\.(\*|[a-z_]+))$"
TOPIC = "tally.core.events"


class CreateWebhookEndpoint(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    url: Annotated[str, Field(min_length=8, max_length=2048)]
    enabled_events: Annotated[
        list[Annotated[str, Field(pattern=EVENT_PATTERN)]], Field(min_length=1, max_length=50)
    ]


class WebhookEndpointView(BaseModel):
    endpoint_id: UUID
    url: str
    enabled_events: list[str]
    status: str
    created_at: str
    secret: str | None = None


class DeliveryView(BaseModel):
    delivery_id: UUID
    event_id: UUID
    event_type: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    created_at: str
    delivered_at: str | None


def endpoint_view(row: asyncpg.Record, secret: str | None = None) -> WebhookEndpointView:
    return WebhookEndpointView(
        endpoint_id=row["endpoint_id"],
        url=row["url"],
        enabled_events=list(row["enabled_events"]),
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        secret=secret,
    )


def delivery_view(row: asyncpg.Record) -> DeliveryView:
    return DeliveryView(
        delivery_id=row["delivery_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        status=row["status"],
        attempts=row["attempts"],
        last_status_code=row["last_status_code"],
        last_error=row["last_error"],
        created_at=row["created_at"].isoformat(),
        delivered_at=row["delivered_at"].isoformat() if row["delivered_at"] else None,
    )


class SecretCipher(Protocol):
    def encrypt(self, key_id: str, secret: bytes) -> bytes: ...

    def decrypt(self, key_id: str, ciphertext: bytes) -> bytes: ...


async def create_endpoint(
    pool: asyncpg.Pool,
    cipher: SecretCipher,
    merchant_id: str,
    body: CreateWebhookEndpoint,
    trusted_hosts: Sequence[str] = (),
) -> tuple[asyncpg.Record, str]:
    parse_webhook_url(body.url, trusted_hosts)  # raises UnsafeDestination
    endpoint_id = uuid4()
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
        row = await connection.fetchrow(
            """INSERT INTO webhook_endpoints(
                   endpoint_id, merchant_id, url, secret_ciphertext, enabled_events
               ) VALUES ($1, $2, $3, $4, $5) RETURNING *""",
            endpoint_id,
            merchant_id,
            body.url,
            cipher.encrypt(f"webhook:{endpoint_id}", secret.encode()),
            sorted(set(body.enabled_events)),
        )
    assert row is not None
    return row, secret


def event_matches(patterns: Sequence[str], event_type: str) -> bool:
    return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in patterns)


def event_envelope(
    event_id: UUID, event_type: str, created_at: str, payload: object
) -> dict[str, object]:
    return {"id": str(event_id), "type": event_type, "created_at": created_at, "data": payload}


class EventPublisher(Protocol):
    async def publish(self, events: list[dict[str, object]]) -> None: ...


def kafka_security_from_env() -> dict[str, Any]:
    """PLAINTEXT locally; SASL_SSL with SCRAM-SHA-512 against MSK (TLS in transit)."""
    protocol = os.environ.get("TALLY_KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
    if protocol == "PLAINTEXT":
        return {}
    import ssl

    options: dict[str, Any] = {
        "security_protocol": protocol,
        "ssl_context": ssl.create_default_context(),
    }
    if protocol == "SASL_SSL":
        options |= {
            "sasl_mechanism": "SCRAM-SHA-512",
            "sasl_plain_username": os.environ["TALLY_KAFKA_USERNAME"],
            "sasl_plain_password": os.environ["TALLY_KAFKA_PASSWORD"],
        }
    return options


class KafkaPublisher:
    """Idempotent Kafka producer; consumers deduplicate on the envelope ``id``."""

    def __init__(self, bootstrap_servers: str, topic: str = TOPIC) -> None:
        from aiokafka import AIOKafkaProducer

        self.topic = topic
        self._producer: Any = AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            enable_idempotence=True,
            acks="all",
            **kafka_security_from_env(),
        )
        self._started = False

    async def publish(self, events: list[dict[str, object]]) -> None:
        if not self._started:
            await self._producer.start()
            self._started = True
        futures = [
            await self._producer.send(
                self.topic,
                key=str(event["aggregate_id"]).encode(),
                value=json.dumps(event, separators=(",", ":")).encode(),
            )
            for event in events
        ]
        for future in futures:
            await future

    async def close(self) -> None:
        if self._started:
            await self._producer.stop()


async def relay_outbox(
    pool: asyncpg.Pool, publisher: EventPublisher | None = None, limit: int = 200
) -> int:
    """Publish unpublished outbox rows and create webhook deliveries, at least once."""
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            """SELECT event_id, aggregate_id, aggregate_type, merchant_id, event_type, payload,
                      created_at
               FROM core_outbox WHERE published_at IS NULL
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $1""",
            limit,
        )
        if not rows:
            return 0
        envelopes = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            envelope = event_envelope(
                row["event_id"], row["event_type"], row["created_at"].isoformat(), payload
            )
            envelopes.append(
                {
                    **envelope,
                    "aggregate_id": str(row["aggregate_id"]),
                    "aggregate_type": row["aggregate_type"],
                    "merchant_id": row["merchant_id"],
                }
            )
        if publisher is not None:
            # A publish failure rolls back; rows stay unpublished and are retried.
            await publisher.publish(envelopes)
        endpoints = await connection.fetch(
            """SELECT endpoint_id, merchant_id, enabled_events FROM webhook_endpoints
               WHERE status = 'active' AND merchant_id = ANY($1::text[])""",
            sorted({row["merchant_id"] for row in rows}),
        )
        deliveries: list[tuple[UUID, str, UUID, str, str]] = []
        for row, envelope in zip(rows, envelopes, strict=True):
            for endpoint in endpoints:
                if endpoint["merchant_id"] == row["merchant_id"] and event_matches(
                    endpoint["enabled_events"], row["event_type"]
                ):
                    body = {key: envelope[key] for key in ("id", "type", "created_at", "data")}
                    deliveries.append(
                        (
                            endpoint["endpoint_id"],
                            row["merchant_id"],
                            row["event_id"],
                            row["event_type"],
                            json.dumps(body, separators=(",", ":")),
                        )
                    )
        if deliveries:
            await connection.executemany(
                """INSERT INTO webhook_deliveries(
                       endpoint_id, merchant_id, event_id, event_type, payload
                   ) VALUES ($1, $2, $3, $4, $5::jsonb)
                   ON CONFLICT (endpoint_id, event_id) DO NOTHING""",
                deliveries,
            )
        await connection.execute(
            """UPDATE core_outbox SET published_at = clock_timestamp(),
                   publish_attempts = publish_attempts + 1
               WHERE event_id = ANY($1::uuid[])""",
            [row["event_id"] for row in rows],
        )
    return len(rows)


@dataclass(slots=True)
class WebhookDispatcher:
    pool: asyncpg.Pool
    http: httpx.AsyncClient
    cipher: SecretCipher
    resolver: Resolver = system_resolver
    trusted_hosts: Sequence[str] = ()
    clock: Callable[[], float] = time.time

    async def claim(self, limit: int) -> list[asyncpg.Record]:
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """SELECT d.*, e.url, e.secret_ciphertext, e.status AS endpoint_status
                   FROM webhook_deliveries d JOIN webhook_endpoints e USING (endpoint_id)
                   WHERE d.status IN ('pending', 'failed')
                     AND d.next_attempt_at <= clock_timestamp()
                     AND (d.lease_until IS NULL OR d.lease_until <= clock_timestamp())
                   ORDER BY d.next_attempt_at FOR UPDATE OF d SKIP LOCKED LIMIT $1""",
                limit,
            )
            if rows:
                await connection.execute(
                    """UPDATE webhook_deliveries
                       SET lease_until = clock_timestamp() + interval '60 seconds'
                       WHERE delivery_id = ANY($1::uuid[])""",
                    [row["delivery_id"] for row in rows],
                )
            return cast(list[asyncpg.Record], rows)

    async def deliver(self, row: Any) -> str:
        payload = row["payload"]
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        started = time.monotonic()
        status_code: int | None = None
        error: str | None = None
        if row["endpoint_status"] != "active":
            error = "endpoint_disabled"
        else:
            try:
                target = parse_webhook_url(row["url"], self.trusted_hosts)
                address = await vetted_address(target, self.resolver)
                secret = self.cipher.decrypt(
                    f"webhook:{row['endpoint_id']}", bytes(row["secret_ciphertext"])
                )
                host_header = (
                    target.host if target.port in (80, 443) else (f"{target.host}:{target.port}")
                )
                connect_host = f"[{address}]" if ":" in address else address
                response = await self.http.post(
                    f"{target.scheme}://{connect_host}:{target.port}{target.path}",
                    content=body,
                    headers={
                        "Host": host_header,
                        "Content-Type": "application/json",
                        "User-Agent": "Tally-Webhooks/1.0",
                        "Tally-Event-Id": str(row["event_id"]),
                        HEADER: sign_payload(secret, body, int(self.clock())),
                    },
                    extensions={"sni_hostname": target.host},
                    follow_redirects=False,
                    timeout=5.0,
                )
                status_code = response.status_code
            except UnsafeDestination as exc:
                error = f"blocked_destination: {exc}"
            except httpx.HTTPError as exc:
                error = f"transport_error: {type(exc).__name__}"
        duration_ms = int((time.monotonic() - started) * 1000)
        if error and error.startswith("blocked_destination"):
            SSRF_BLOCKED.inc()
        succeeded = status_code is not None and 200 <= status_code < 300
        attempts = int(row["attempts"]) + 1
        if succeeded:
            status = "succeeded"
        elif error and error.startswith(("blocked_destination", "endpoint_disabled")):
            status = "dead"
        else:
            status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
            error = error or f"http_status_{status_code}"
        WEBHOOK_RESULTS.labels(status).inc()
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO webhook_delivery_attempts(
                       delivery_id, merchant_id, status_code, error, duration_ms
                   ) VALUES ($1, $2, $3, $4, $5)""",
                row["delivery_id"],
                row["merchant_id"],
                status_code,
                error,
                duration_ms,
            )
            await connection.execute(
                """UPDATE webhook_deliveries SET status = $2, attempts = $3,
                       last_status_code = $4, last_error = $5, lease_until = NULL,
                       delivered_at = CASE WHEN $2 = 'succeeded' THEN clock_timestamp() END,
                       next_attempt_at = clock_timestamp() + $6::interval
                   WHERE delivery_id = $1""",
                row["delivery_id"],
                status,
                attempts,
                status_code,
                error,
                timedelta(seconds=min(6 * 3600, 30 * 2 ** (attempts - 1))),
            )
        return status

    async def run_batch(self, limit: int = 50) -> int:
        rows = await self.claim(limit)
        for row in rows:
            await self.deliver(row)
        return len(rows)


async def redeliver(pool: asyncpg.Pool, merchant_id: str, delivery_id: UUID) -> asyncpg.Record:
    row = await pool.fetchrow(
        """UPDATE webhook_deliveries SET status = 'pending', next_attempt_at = clock_timestamp(),
               attempts = 0, lease_until = NULL
           WHERE delivery_id = $1 AND merchant_id = $2 RETURNING *""",
        delivery_id,
        merchant_id,
    )
    if row is None:
        raise KeyError("delivery not found")
    return row


async def queue_test_event(pool: asyncpg.Pool, merchant_id: str, endpoint_id: UUID) -> UUID:
    """Queue a signed ``webhook.test`` event for one endpoint (signature testing)."""
    event_id = uuid4()
    body = event_envelope(event_id, "webhook.test", "now", {"endpoint_id": str(endpoint_id)})
    row = await pool.fetchrow(
        """INSERT INTO webhook_deliveries(endpoint_id, merchant_id, event_id, event_type, payload)
           SELECT endpoint_id, merchant_id, $3, 'webhook.test', $4::jsonb
           FROM webhook_endpoints WHERE endpoint_id = $1 AND merchant_id = $2
           RETURNING delivery_id""",
        endpoint_id,
        merchant_id,
        event_id,
        json.dumps(body),
    )
    if row is None:
        raise KeyError("endpoint not found")
    return cast(UUID, row["delivery_id"])

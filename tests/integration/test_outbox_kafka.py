"""The outbox relay publishes committed events to Kafka (Redpanda locally)."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import pytest
from chaos.stack import build_stack, provision_databases
from services.core.webhooks import KafkaPublisher, relay_outbox


def test_outbox_events_reach_kafka_exactly_as_committed() -> None:
    bootstrap = os.environ.get("TALLY_KAFKA_BOOTSTRAP")
    if os.environ.get("TALLY_STACK_TESTS") != "1" or not bootstrap:
        pytest.skip("set TALLY_STACK_TESTS=1 and TALLY_KAFKA_BOOTSTRAP to run the Kafka test")

    async def exercise() -> None:
        from aiokafka import AIOKafkaConsumer

        general, ledger, vault = await provision_databases("tally_it_kafka")
        stack = await build_stack(
            general, ledger, vault, os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        )
        topic = f"tally.test.{uuid4().hex}"
        publisher = KafkaPublisher(bootstrap, topic=topic)
        try:
            created = await stack.send(
                "POST",
                "/v1/payment_intents",
                {
                    "amount_minor": 1_234,
                    "currency": "INR",
                    "payment_method_type": "upi",
                    "payer_vpa": "payer@bank-a",
                    "payee_vpa": "merchant@bank-b",
                },
                "kafka-create",
            )
            payment_id = created.json()["payment_id"]
            await stack.send(
                "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, "kafka-confirm"
            )
            expected = {
                str(row["event_id"])
                for row in await stack.general_pool.fetch("SELECT event_id FROM core_outbox")
            }
            assert await relay_outbox(stack.general_pool, publisher) == len(expected)
            assert await relay_outbox(stack.general_pool, publisher) == 0

            consumer = AIOKafkaConsumer(
                topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest"
            )
            await consumer.start()
            seen: dict[str, dict[str, object]] = {}
            try:
                while len(seen) < len(expected):
                    batch = await consumer.getmany(timeout_ms=5_000)
                    if not batch:
                        break
                    for messages in batch.values():
                        for message in messages:
                            event = json.loads(message.value)
                            seen[event["id"]] = event
            finally:
                await consumer.stop()
            assert set(seen) == expected
            assert {e["type"] for e in seen.values()} >= {
                "payment_intent.created",
                "payment_intent.succeeded",
            }
            assert all(e["aggregate_id"] == payment_id for e in seen.values())
        finally:
            await publisher.close()
            await stack.close()

    asyncio.run(exercise())

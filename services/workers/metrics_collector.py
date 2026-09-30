"""Publish database-derived gauges (queues, ages, backlogs) for dashboards and alerts."""

from __future__ import annotations

from typing import Any

import asyncpg
from libs.observability.metrics import (
    BREAKER_OPEN,
    OUTBOX_BACKLOG,
    PAYMENTS_BY_STATE,
    PENDING_UNKNOWN_OLDEST,
    RECON_OPEN_BREAKS,
    RECOVERY_LAG,
    REVIEW_QUEUE,
    WEBHOOK_BACKLOG,
    WEBHOOK_OLDEST,
)

STATES = (
    "created",
    "risk_review",
    "authorizing",
    "authorized",
    "capturing",
    "succeeded",
    "failed",
    "pending_unknown",
    "reversal_pending",
    "reversed",
    "cancelled",
    "expired",
)


async def collect(pool: asyncpg.Pool, state: Any = None) -> None:
    counts = {
        row["status"]: row["n"]
        for row in await pool.fetch(
            "SELECT status, count(*) AS n FROM payment_intents GROUP BY status"
        )
    }
    for name in STATES:
        PAYMENTS_BY_STATE.labels(name).set(counts.get(name, 0))
    row = await pool.fetchrow(
        """SELECT
             coalesce(extract(epoch FROM clock_timestamp() - min(updated_at)
                      FILTER (WHERE status IN ('pending_unknown', 'reversal_pending'))), 0)
               AS oldest_unknown,
             coalesce(extract(epoch FROM clock_timestamp() - min(next_recovery_at)
                      FILTER (WHERE status IN ('pending_unknown', 'reversal_pending')
                              AND next_recovery_at < clock_timestamp())), 0) AS recovery_lag
           FROM payment_intents"""
    )
    assert row is not None
    PENDING_UNKNOWN_OLDEST.set(float(row["oldest_unknown"]))
    RECOVERY_LAG.set(float(row["recovery_lag"]))
    webhooks = await pool.fetchrow(
        """SELECT count(*) AS n,
                  coalesce(extract(epoch FROM clock_timestamp() - min(created_at)), 0) AS oldest
           FROM webhook_deliveries WHERE status IN ('pending', 'failed')"""
    )
    assert webhooks is not None
    WEBHOOK_BACKLOG.set(webhooks["n"])
    WEBHOOK_OLDEST.set(float(webhooks["oldest"]))
    OUTBOX_BACKLOG.set(
        await pool.fetchval("SELECT count(*) FROM core_outbox WHERE published_at IS NULL")
    )
    for kind in (
        "missing_at_bank",
        "missing_internally",
        "amount_mismatch",
        "duplicate",
        "status_mismatch",
        "timing_difference",
        "fee_tax_mismatch",
        "unknown",
    ):
        RECON_OPEN_BREAKS.labels(kind).set(0)
    for breaks in await pool.fetch(
        "SELECT break_type, count(*) AS n FROM recon_breaks WHERE status = 'open' "
        "GROUP BY break_type"
    ):
        RECON_OPEN_BREAKS.labels(breaks["break_type"]).set(breaks["n"])
    REVIEW_QUEUE.set(
        await pool.fetchval("SELECT count(*) FROM risk_review_cases WHERE status = 'open'")
    )
    if state is not None:
        for name in (
            "bank_breaker",
            "card_network_breaker",
            "bank_status_breaker",
            "card_network_status_breaker",
        ):
            breaker = getattr(state, name, None)
            if breaker is not None:
                BREAKER_OPEN.labels(name.removesuffix("_breaker")).set(1 if breaker.is_open else 0)

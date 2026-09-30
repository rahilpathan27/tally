"""AML-lite monitoring: rule-based detectors that raise alerts for human review.

DEMONSTRATION ONLY. These rules illustrate the shape of transaction monitoring; they are not a
compliance programme, are not tuned, and the sanctions list is a synthetic stub.

* structuring: one payer with several payments just under a reporting threshold within 24 hours;
* rapid in-out: a VPA receives funds and pays out a similar amount within an hour (pass-through);
* merchant volume spike: a merchant's last-24h volume exceeds 5x its trailing 7-day daily average;
* sanctions match: a VPA or merchant name matches the (synthetic) list after normalisation.

Alerts are de-duplicated by a deterministic key, so running the detectors again is idempotent.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

STRUCTURING_THRESHOLD_MINOR = 5_000_000  # ₹50,000
STRUCTURING_BAND_MINOR = 500_000  # payments within ₹5,000 below the threshold
STRUCTURING_MIN_COUNT = 3
SPIKE_MULTIPLIER = 5


def normalise(name: str) -> str:
    local = name.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9]", "", local)


async def _raise(
    connection: asyncpg.Connection,
    alert_type: str,
    subject: str,
    merchant_id: str | None,
    details: dict[str, Any],
    dedupe_key: str,
) -> bool:
    created = await connection.fetchval(
        """INSERT INTO aml_alerts(alert_id, alert_type, subject, merchant_id, details, dedupe_key)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING true""",
        uuid.uuid4(),
        alert_type,
        subject,
        merchant_id,
        json.dumps(details, default=str),
        dedupe_key,
    )
    return bool(created)


async def run_detectors(pool: asyncpg.Pool, now: datetime | None = None) -> dict[str, int]:
    at = now or datetime.now(UTC)
    day = at.date().isoformat()
    counts = {"structuring": 0, "rapid_in_out": 0, "merchant_volume_spike": 0, "sanctions_match": 0}
    async with pool.acquire() as connection, connection.transaction():
        structuring = await connection.fetch(
            """SELECT coalesce(payer_vpa, payment_method_token) AS payer, merchant_id,
                      count(*) AS n, sum(amount_minor) AS total
               FROM payment_intents
               WHERE status = 'succeeded' AND created_at > $1::timestamptz - interval '24 hours'
                 AND amount_minor BETWEEN $2 AND $3
               GROUP BY 1, 2 HAVING count(*) >= $4""",
            at,
            STRUCTURING_THRESHOLD_MINOR - STRUCTURING_BAND_MINOR,
            STRUCTURING_THRESHOLD_MINOR - 1,
            STRUCTURING_MIN_COUNT,
        )
        for row in structuring:
            counts["structuring"] += await _raise(
                connection,
                "structuring",
                row["payer"],
                row["merchant_id"],
                {"payments": row["n"], "total_minor": row["total"]},
                f"structuring:{row['payer']}:{day}",
            )
        rapid = await connection.fetch(
            """SELECT i.payee_vpa AS vpa, sum(i.amount_minor) AS received,
                      (SELECT sum(o.amount_minor) FROM payment_intents o
                       WHERE o.payer_vpa = i.payee_vpa AND o.status = 'succeeded'
                         AND o.created_at BETWEEN min(i.created_at)
                             AND min(i.created_at) + interval '1 hour') AS sent
               FROM payment_intents i
               WHERE i.status = 'succeeded' AND i.payment_method_type = 'upi'
                 AND i.created_at > $1::timestamptz - interval '24 hours'
               GROUP BY i.payee_vpa""",
            at,
        )
        for row in rapid:
            received, sent = int(row["received"] or 0), int(row["sent"] or 0)
            if received and sent and sent * 10 >= received * 8:
                counts["rapid_in_out"] += await _raise(
                    connection,
                    "rapid_in_out",
                    row["vpa"],
                    None,
                    {"received_minor": received, "sent_within_hour_minor": sent},
                    f"rapid:{row['vpa']}:{day}",
                )
        spikes = await connection.fetch(
            """WITH recent AS (
                   SELECT merchant_id, sum(amount_minor) AS volume FROM payment_intents
                   WHERE status = 'succeeded' AND created_at > $1::timestamptz - interval '24 hours'
                   GROUP BY merchant_id),
               baseline AS (
                   SELECT merchant_id, sum(amount_minor) / 7 AS daily FROM payment_intents
                   WHERE status = 'succeeded' AND created_at > $1::timestamptz - interval '8 days'
                     AND created_at <= $1::timestamptz - interval '24 hours'
                   GROUP BY merchant_id)
               SELECT r.merchant_id, r.volume, t.daily FROM recent r
               JOIN baseline t USING (merchant_id)
               WHERE t.daily > 0 AND r.volume > t.daily * $2""",
            at,
            SPIKE_MULTIPLIER,
        )
        for row in spikes:
            counts["merchant_volume_spike"] += await _raise(
                connection,
                "merchant_volume_spike",
                row["merchant_id"],
                row["merchant_id"],
                {"last_24h_minor": row["volume"], "trailing_daily_minor": int(row["daily"])},
                f"spike:{row['merchant_id']}:{day}",
            )
        names = {
            r["name_normalized"]
            for r in await connection.fetch("SELECT name_normalized FROM sanctions_list")
        }
        parties = await connection.fetch(
            """SELECT DISTINCT party, merchant_id FROM (
                   SELECT payer_vpa AS party, merchant_id FROM payment_intents
                   WHERE created_at > $1::timestamptz - interval '24 hours'
                     AND payer_vpa IS NOT NULL
                   UNION SELECT payee_vpa, merchant_id FROM payment_intents
                   WHERE created_at > $1::timestamptz - interval '24 hours'
                     AND payee_vpa IS NOT NULL
                   UNION SELECT display_name, merchant_id FROM merchants) p""",
            at,
        )
        for row in parties:
            if normalise(str(row["party"])) in names:
                counts["sanctions_match"] += await _raise(
                    connection,
                    "sanctions_match",
                    row["party"],
                    row["merchant_id"],
                    {"list": "synthetic-demo", "normalised": normalise(str(row["party"]))},
                    f"sanctions:{row['party']}",
                )
    return counts


async def open_case(
    pool: asyncpg.Pool, alert_ids: list[uuid.UUID], summary: str, actor: str
) -> uuid.UUID:
    case_id = uuid.uuid4()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """INSERT INTO aml_cases(case_id, status, summary, created_by)
               VALUES ($1, 'open', $2, $3)""",
            case_id,
            summary,
            actor,
        )
        await connection.execute(
            """UPDATE aml_alerts SET status = 'escalated', case_id = $2
               WHERE alert_id = ANY($1::uuid[]) AND status = 'open'""",
            alert_ids,
            case_id,
        )
    return case_id

"""Scheduled retraining: add production feedback labels, train, gate, register as challenger.

Labels come from chargebacks and analyst review outcomes (``risk_labels``) joined to the exact
feature vector the service used for that payment (``risk_decisions.features``), so feedback rows
have train/serve parity by construction. The candidate becomes the shadow challenger only if it
passes the evaluation gate against the current champion; promotion to champion still needs
maker-checker approval through the risk API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
from services.risk import registry
from services.risk.features import FEATURE_NAMES

from ml.train import train


async def feedback(pool: asyncpg.Pool) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    rows = await pool.fetch(
        """SELECT DISTINCT ON (d.payment_id) d.features, l.label
           FROM risk_decisions d JOIN risk_labels l ON l.payment_id::text = d.payment_id
           ORDER BY d.payment_id, l.created_at DESC"""
    )
    vectors, labels = [], []
    for row in rows:
        features = row["features"]
        features = json.loads(features) if isinstance(features, str) else features
        vectors.append([float(features[name]) for name in FEATURE_NAMES])
        labels.append(1 if row["label"] == "fraud" else 0)
    return np.asarray(vectors, dtype=np.float32).reshape(-1, len(FEATURE_NAMES)), np.asarray(
        labels, dtype=np.int32
    )


async def retrain(
    database_url: str, version: str, output_root: Path, days: int, customers: int, seed: int
) -> dict[str, Any]:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        extra = await feedback(pool)
        metadata = await asyncio.to_thread(
            train, days, customers, seed, output_root / version, version, extra
        )
        await registry.register_model(pool, output_root / version, "retrain-job")
        champion = await pool.fetchval(
            "SELECT metrics FROM risk_model_versions WHERE stage = 'champion'"
        )
        champion_metrics = None if champion is None else json.loads(champion)
        failures = registry.evaluation_gate(metadata["metrics"], champion_metrics)
        if not failures:
            await registry.promote_challenger(pool, version, "retrain-job")
        return {
            "version": version,
            "feedback_rows": int(len(extra[0])),
            "gate_passed": not failures,
            "gate_failures": failures,
            "pr_auc": metadata["metrics"]["pr_auc"],
        }
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", default="ml/artifacts")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--customers", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    url = os.environ.get(
        "TALLY_DATABASE_URL", "postgresql://tally:tally-local-only@127.0.0.1:55432/tally"
    )
    result = asyncio.run(
        retrain(url, args.version, Path(args.output), args.days, args.customers, args.seed)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

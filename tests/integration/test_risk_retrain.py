"""The retraining job uses production labels and respects the evaluation gate."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from chaos.stack import provision_databases
from ml.retrain import retrain
from services.risk import registry
from services.risk.features import FEATURE_NAMES
from services.risk.rules import DEFAULT_RULES


def test_retrain_registers_candidate_with_feedback_and_gate(tmp_path: Path) -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        import asyncpg

        general, _, _ = await provision_databases("tally_it_retrain")
        pool = await asyncpg.create_pool(general, min_size=1, max_size=2)
        try:
            await registry.bootstrap(pool, Path("ml/artifacts/fraud-gbm-v1"), DEFAULT_RULES)
            await pool.execute("INSERT INTO merchants(merchant_id, display_name) VALUES ('m', 'm')")
            for index in range(3):
                payment_id = uuid4()
                await pool.execute(
                    """INSERT INTO payment_intents(payment_id, merchant_id, amount_minor, currency,
                           payment_method_type, payer_vpa, payee_vpa, status, mode)
                       VALUES ($1, 'm', 1000, 'INR', 'upi', 'payer@bank-a',
                               'merchant@bank-b', 'failed', 'test')""",
                    payment_id,
                )
                await pool.execute(
                    """INSERT INTO risk_decisions(decision_id, payment_id, merchant_id, decision,
                           features, amount_minor, method, latency_us)
                       VALUES ($1, $2, 'm', 'review', $3::jsonb, 1000, 'upi', 1)""",
                    uuid4(),
                    str(payment_id),
                    json.dumps({name: float(index) for name in FEATURE_NAMES}),
                )
                await pool.execute(
                    """INSERT INTO risk_labels(payment_id, merchant_id, label, source, source_id)
                       VALUES ($1, 'm', 'fraud', 'analyst_review', $2)""",
                    payment_id,
                    str(uuid4()),
                )
        finally:
            await pool.close()
        result = await retrain(general, "fraud-gbm-candidate", tmp_path, 24, 6_000, 5)
        assert result["feedback_rows"] == 3
        pool = await asyncpg.create_pool(general, min_size=1, max_size=2)
        try:
            stage = await pool.fetchval(
                "SELECT stage FROM risk_model_versions WHERE version = 'fraud-gbm-candidate'"
            )
            champion = await pool.fetchval(
                "SELECT version FROM risk_model_versions WHERE stage = 'champion'"
            )
        finally:
            await pool.close()
        assert champion == "fraud-gbm-v1"  # retraining never replaces the champion by itself
        assert stage == ("challenger" if result["gate_passed"] else "registered")
        if not result["gate_passed"]:
            assert result["gate_failures"]
        metadata = json.loads((tmp_path / "fraud-gbm-candidate" / "metadata.json").read_text())
        assert metadata["feedback_rows"] == 3

    asyncio.run(exercise())

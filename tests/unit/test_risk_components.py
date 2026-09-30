import asyncio
import copy
from pathlib import Path

import numpy as np
import pytest
from services.risk.drift import drift_report, psi
from services.risk.features import FEATURE_NAMES, MemoryFeatureState, compute_features
from services.risk.model import REASON_CODES, ModelRuntime
from services.risk.registry import evaluation_gate
from services.risk.rules import DEFAULT_RULES, RuleSet, RuleSetError

MODEL = Path("ml/artifacts/fraud-gbm-v1")


def test_rule_dsl_evaluates_and_rejects_malformed_sets() -> None:
    rules = RuleSet(DEFAULT_RULES)
    hits = rules.evaluate(
        {"is_card": 1, "device_distinct_instruments_24h": 5, "instrument_count_1h": 0}
    )
    assert [h.rule_id for h in hits] == ["R002"]
    listed = copy.deepcopy(DEFAULT_RULES)
    listed["lists"] = {"blocklist": {"device_id": ["bad"]}, "allowlist": {"instrument_id": ["vip"]}}
    rules = RuleSet(listed)
    assert {h.action for h in rules.evaluate({"device_id": "bad"})} == {"block"}
    assert {h.action for h in rules.evaluate({"instrument_id": "vip"})} == {"allow"}
    for bad in (
        {"rules": [{"rule_id": "x", "action": "explode", "reason_code": "r", "condition": {}}]},
        {
            "rules": [
                {
                    "rule_id": "x",
                    "action": "block",
                    "reason_code": "r",
                    "condition": {"fact": "a", "op": "~", "value": 1},
                }
            ]
        },
        {
            "rules": [
                {
                    "rule_id": "x",
                    "action": "block",
                    "reason_code": "r",
                    "condition": {"in_list": "blocklist", "field": "password"},
                }
            ]
        },
        {
            "rules": [
                {
                    "rule_id": "x",
                    "action": "block",
                    "reason_code": "r",
                    "condition": {"fact": "a", "op": ">", "value": 1},
                }
            ]
            * 2
        },
    ):
        with pytest.raises(RuleSetError):
            RuleSet(bad)


def test_psi_is_zero_for_identical_and_large_for_shifted_distributions() -> None:
    rng = np.random.default_rng(1)
    baseline_values = rng.normal(0, 1, 50_000)
    edges = np.quantile(baseline_values, np.linspace(0, 1, 11))
    counts, _ = np.histogram(baseline_values, bins=edges)
    baseline = {"edges": edges.tolist(), "proportions": (counts / counts.sum()).tolist()}
    assert psi(baseline, rng.normal(0, 1, 20_000).tolist()) < 0.01
    shifted = drift_report({"x": baseline}, {"x": rng.normal(1.5, 1, 20_000).tolist()})
    assert shifted[0]["status"] == "alert" and shifted[0]["ks"] > 0.3


def test_evaluation_gate_blocks_regressions() -> None:
    champion = {"pr_auc": 0.88, "recall_at_fpr_0.01": 0.92, "onnx_max_abs_diff": 1e-7}
    assert evaluation_gate({**champion, "pr_auc": 0.87}, champion) == []
    assert evaluation_gate({**champion, "pr_auc": 0.80}, champion)
    assert evaluation_gate({**champion, "onnx_max_abs_diff": 0.01}, None)


def test_model_runtime_scores_with_reason_codes() -> None:
    runtime = ModelRuntime(MODEL)
    state = MemoryFeatureState()
    from datetime import UTC, datetime

    from services.risk.features import RiskEvent

    event = RiskEvent(
        "p",
        "m",
        datetime(2026, 9, 30, 12, tzinfo=UTC),
        1_500_000,
        "upi",
        "u@a",
        "x@b",
        "d",
        "1.1.1.1",
        "RU",
        "IN",
        3,
    )
    features = asyncio.run(compute_features(event, state))
    assert list(features) == list(FEATURE_NAMES)
    score = runtime.score(features)
    assert 0.0 <= score.calibrated <= 1.0 and 0.0 <= score.raw <= 1.0
    assert all(r.code in {code for code, _ in REASON_CODES.values()} for r in score.reasons)
    assert runtime.decision(1.0, "card") == "block"
    assert runtime.decision(0.0, "upi") == "allow"
    assert runtime.decision(runtime.thresholds["review"], "card") in {"step_up", "block"}

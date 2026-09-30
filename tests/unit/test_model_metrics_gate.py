"""CI gate on the committed champion: metrics must not regress below published floors."""

import json
from pathlib import Path

FLOORS = {
    "pr_auc": 0.85,
    "roc_auc": 0.95,
    "recall_at_fpr_0.01": 0.88,
    "precision_at_block_threshold": 0.9,
}


def test_committed_champion_meets_metric_floors() -> None:
    metadata = json.loads(Path("ml/artifacts/fraud-gbm-v1/metadata.json").read_text())
    metrics = metadata["metrics"]
    for name, floor in FLOORS.items():
        assert metrics[name] >= floor, (name, metrics[name], floor)
    assert metrics["onnx_max_abs_diff"] <= 1e-4
    assert metadata["thresholds"]["block"] >= metadata["thresholds"]["review"]
    assert metadata["data"]["split"].startswith("train first")

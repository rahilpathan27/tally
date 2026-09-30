"""Train, calibrate, evaluate and export the fraud model.

Features come from replaying events through ``services.risk.features`` (the serving code), so the
training matrix is what the service would have computed. Splits are by time (train, validation,
test) to avoid leakage from the future. Artifacts: ``model.onnx`` (served score), ``model.txt``
(LightGBM booster used for exact TreeSHAP reason codes) and ``metadata.json`` (features,
calibration, thresholds, metrics and drift baselines).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from onnxmltools import convert_lightgbm
from onnxmltools.convert.common.data_types import FloatTensorType
from services.risk.features import FEATURE_NAMES, MemoryFeatureState, compute_features, vector
from services.risk.rules import DEFAULT_RULES, SEVERITY, RuleSet
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from ml.datagen import START, LabelledEvent, generate

# Cost model (rupees): a missed fraud loses its amount; a review costs analyst time and friction;
# a wrongly blocked legitimate payment loses the sale and goodwill.
REVIEW_COST_RUPEES = 25
BLOCK_FP_COST_RUPEES = 250
BLOCK_MIN_PRECISION = 0.9


@dataclass(slots=True)
class Dataset:
    X: NDArray[np.float32]
    y: NDArray[np.int32]
    amount_rupees: NDArray[np.float64]
    day: NDArray[np.int32]
    pattern: list[str]
    facts: list[dict[str, Any]]


async def build_dataset(events: list[LabelledEvent]) -> Dataset:
    state = MemoryFeatureState()
    rows: list[list[float]] = []
    facts: list[dict[str, Any]] = []
    for item in events:
        features = await compute_features(item.event, state)
        await state.record(item.event)
        rows.append(vector(features))
        facts.append(
            {
                **features,
                "amount_minor": item.event.amount_minor,
                "device_id": item.event.device_id,
                "instrument_id": item.event.instrument_id,
                "ip_address": item.event.ip_address,
                "payee_id": item.event.payee_id,
                "merchant_id": item.event.merchant_id,
                "ip_country": item.event.ip_country,
            }
        )
    return Dataset(
        X=np.asarray(rows, dtype=np.float32),
        y=np.asarray([int(e.fraud) for e in events], dtype=np.int32),
        amount_rupees=np.asarray([e.event.amount_minor / 100 for e in events]),
        day=np.asarray([(e.event.occurred_at - START).days for e in events], dtype=np.int32),
        pattern=[e.pattern for e in events],
        facts=facts,
    )


def recall_at_fpr(y: NDArray[Any], score: NDArray[Any], fpr: float) -> tuple[float, float, float]:
    """Recall and precision at the threshold whose false-positive rate is at most ``fpr``."""
    legit = np.sort(score[y == 0])[::-1]
    allowed = int(np.floor(fpr * len(legit)))
    threshold = legit[allowed] if allowed < len(legit) else 0.0
    flagged = score > threshold
    tp = int(np.sum(flagged & (y == 1)))
    fp = int(np.sum(flagged & (y == 0)))
    recall = tp / max(1, int(np.sum(y == 1)))
    precision = tp / max(1, tp + fp)
    return float(threshold), recall, precision


def choose_thresholds(
    y: NDArray[Any], score: NDArray[Any], amounts: NDArray[Any]
) -> dict[str, Any]:
    grid = np.unique(np.quantile(score, np.linspace(0.5, 0.9999, 400)))
    best = (float("inf"), 1.0)
    for t in grid:
        flagged = score >= t
        missed = float(np.sum(amounts[(y == 1) & ~flagged]))
        reviews = float(np.sum(flagged)) * REVIEW_COST_RUPEES
        cost = missed + reviews
        if cost < best[0]:
            best = (cost, float(t))
    review = best[1]
    block = 1.0
    for t in sorted(grid):
        if t < review:
            continue
        flagged = score >= t
        tp = int(np.sum(flagged & (y == 1)))
        if tp and tp / max(1, int(np.sum(flagged))) >= BLOCK_MIN_PRECISION:
            block = float(t)
            break
    return {"review": review, "block": max(block, review), "validation_cost_rupees": best[0]}


def total_cost(
    y: NDArray[Any], score: NDArray[Any], amounts: NDArray[Any], th: dict[str, Any]
) -> float:
    blocked = score >= th["block"]
    reviewed = (score >= th["review"]) & ~blocked
    missed = float(np.sum(amounts[(y == 1) & ~blocked & ~reviewed]))
    return (
        missed
        + float(np.sum(reviewed)) * REVIEW_COST_RUPEES
        + float(np.sum(blocked & (y == 0))) * BLOCK_FP_COST_RUPEES
    )


def histogram_baseline(values: NDArray[Any], bins: int = 10) -> dict[str, list[float]]:
    edges = np.unique(np.quantile(values, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        edges = np.array([values.min() - 1, values.min(), values.max() + 1])
    counts, _ = np.histogram(values, bins=edges)
    return {"edges": edges.tolist(), "proportions": (counts / max(1, counts.sum())).tolist()}


def expected_calibration_error(y: NDArray[Any], p: NDArray[Any], bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    error = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if mask.any():
            error += abs(float(p[mask].mean()) - float(y[mask].mean())) * mask.mean()
    return float(error)


def train(
    days: int,
    customers: int,
    seed: int,
    output: Path,
    version: str,
    extra: tuple[NDArray[np.float32], NDArray[np.int32]] | None = None,
) -> dict[str, Any]:
    """Train and export one model. ``extra`` rows (production feedback) join the training split."""
    started = time.monotonic()
    events = generate(days=days, customers=customers, seed=seed)
    data = asyncio.run(build_dataset(events))
    train_mask = data.day < int(days * 2 / 3)
    valid_mask = (data.day >= int(days * 2 / 3)) & (data.day < int(days * 5 / 6))
    test_mask = data.day >= int(days * 5 / 6)
    X_train, y_train = data.X[train_mask], data.y[train_mask]
    if extra is not None and len(extra[0]):
        X_train = np.vstack([X_train, extra[0]])
        y_train = np.concatenate([y_train, extra[1]])
    positives = max(1, int(y_train.sum()))
    model = lgb.LGBMClassifier(
        n_estimators=600,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=40,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        scale_pos_weight=min(20.0, (len(y_train) - positives) / positives),
        random_state=seed,
        verbose=-1,
    )
    model.fit(
        X_train,
        y_train,
        eval_X=(data.X[valid_mask],),
        eval_y=(data.y[valid_mask],),
        eval_metric="average_precision",
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    raw_valid = np.asarray(model.predict_proba(data.X[valid_mask]))[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(raw_valid, data.y[valid_mask])
    calibrated_valid = calibrator.predict(raw_valid)
    thresholds = choose_thresholds(
        data.y[valid_mask], calibrated_valid, data.amount_rupees[valid_mask]
    )

    raw_test = np.asarray(model.predict_proba(data.X[test_mask]))[:, 1]
    calibrated_test = calibrator.predict(raw_test)
    y_test = data.y[test_mask]
    amounts_test = data.amount_rupees[test_mask]
    patterns_test = [p for p, keep in zip(data.pattern, test_mask, strict=True) if keep]
    flagged = calibrated_test >= thresholds["review"]
    blocked = calibrated_test >= thresholds["block"]
    metrics: dict[str, Any] = {
        "pr_auc": float(average_precision_score(y_test, raw_test)),
        "roc_auc": float(roc_auc_score(y_test, raw_test)),
        "brier_calibrated": float(brier_score_loss(y_test, calibrated_test)),
        "ece_calibrated": expected_calibration_error(y_test, calibrated_test),
        "fraud_rate_test": float(y_test.mean()),
        "test_rows": int(test_mask.sum()),
        "train_rows": int(train_mask.sum()),
        "validation_rows": int(valid_mask.sum()),
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "recall_at_review_threshold": float(flagged[y_test == 1].mean()),
        "precision_at_review_threshold": float(y_test[flagged].mean()) if flagged.any() else 0.0,
        "recall_at_block_threshold": float(blocked[y_test == 1].mean()),
        "precision_at_block_threshold": float(y_test[blocked].mean()) if blocked.any() else 0.0,
        "false_positive_rate_review": float(flagged[y_test == 0].mean()),
        "cost_rupees_model": total_cost(y_test, calibrated_test, amounts_test, thresholds),
        "cost_rupees_no_model": float(np.sum(amounts_test[y_test == 1])),
    }
    for fpr in (0.001, 0.01):
        _, recall, precision = recall_at_fpr(y_test, raw_test, fpr)
        metrics[f"recall_at_fpr_{fpr}"] = recall
        metrics[f"precision_at_fpr_{fpr}"] = precision
    metrics["recall_by_pattern_at_review"] = {
        pattern: float(
            np.mean([f for f, p in zip(flagged, patterns_test, strict=True) if p == pattern])
        )
        for pattern in ("card_testing", "account_takeover", "mule")
    }
    # Rules-only baseline on the same test window (hot-reloadable rules, no model).
    rules = RuleSet(DEFAULT_RULES)
    test_facts = [f for f, keep in zip(data.facts, test_mask, strict=True) if keep]
    rule_flags = np.asarray(
        [
            max((SEVERITY[h.action] for h in rules.evaluate(f)), default=0) >= SEVERITY["review"]
            for f in test_facts
        ]
    )
    metrics["rules_only"] = {
        "recall": float(rule_flags[y_test == 1].mean()),
        "precision": float(y_test[rule_flags].mean()) if rule_flags.any() else 0.0,
        "false_positive_rate": float(rule_flags[y_test == 0].mean()),
    }
    # Subgroup check: legitimate customers abroad (geo mismatch) vs at home.
    geo_index = FEATURE_NAMES.index("geo_mismatch")
    legit_abroad = (y_test == 0) & (data.X[test_mask][:, geo_index] == 1)
    legit_home = (y_test == 0) & (data.X[test_mask][:, geo_index] == 0)
    metrics["fpr_legit_geo_mismatch"] = (
        float(flagged[legit_abroad].mean()) if legit_abroad.any() else 0.0
    )
    metrics["fpr_legit_home"] = float(flagged[legit_home].mean()) if legit_home.any() else 0.0
    metrics["legit_geo_mismatch_rows"] = int(legit_abroad.sum())

    output.mkdir(parents=True, exist_ok=True)
    booster = model.booster_
    booster.save_model(str(output / "model.txt"))
    onnx_model = convert_lightgbm(
        booster,
        initial_types=[("features", FloatTensorType([None, len(FEATURE_NAMES)]))],
        zipmap=False,
        target_opset=15,
    )
    (output / "model.onnx").write_bytes(onnx_model.SerializeToString())
    session = ort.InferenceSession(str(output / "model.onnx"))
    onnx_scores = session.run(None, {"features": data.X[test_mask][:2000]})[1][:, 1]
    parity = float(np.max(np.abs(onnx_scores - raw_test[:2000])))
    if parity > 1e-4:
        raise AssertionError(f"ONNX and LightGBM scores diverge by {parity}")
    metrics["onnx_max_abs_diff"] = parity
    baselines = {
        name: histogram_baseline(data.X[train_mask][:, i]) for i, name in enumerate(FEATURE_NAMES)
    }
    baselines["__score__"] = histogram_baseline(
        calibrator.predict(np.asarray(model.predict_proba(X_train))[:, 1])
    )
    metadata = {
        "version": version,
        "trained_at": datetime.now(UTC).isoformat(),
        "algorithm": "LightGBM gradient-boosted trees; isotonic calibration",
        "feature_names": list(FEATURE_NAMES),
        "calibration": {
            "x": calibrator.X_thresholds_.tolist(),
            "y": calibrator.y_thresholds_.tolist(),
        },
        "thresholds": thresholds,
        "cost_model": {
            "review_cost_rupees": REVIEW_COST_RUPEES,
            "block_false_positive_cost_rupees": BLOCK_FP_COST_RUPEES,
            "block_min_precision": BLOCK_MIN_PRECISION,
        },
        "data": {
            "generator": "ml/datagen.py",
            "seed": seed,
            "days": days,
            "customers": customers,
            "split": "train first 2/3 of days, validate next 1/6, test last 1/6",
            "window_start": START.isoformat(),
            "window_end": (START + timedelta(days=days)).isoformat(),
        },
        "metrics": metrics,
        "baselines": baselines,
        "feedback_rows": 0 if extra is None else len(extra[0]),
        "training_seconds": round(time.monotonic() - started, 1),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--customers", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--version", default="fraud-gbm-v1")
    parser.add_argument("--output", default="ml/artifacts")
    args = parser.parse_args()
    metadata = train(
        args.days, args.customers, args.seed, Path(args.output) / args.version, args.version
    )
    print(json.dumps(metadata["metrics"], indent=2))


if __name__ == "__main__":
    main()

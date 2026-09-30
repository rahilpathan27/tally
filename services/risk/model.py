"""Model serving: ONNX Runtime for the score, LightGBM TreeSHAP for reason codes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import onnxruntime as ort

from services.risk.features import FEATURE_NAMES, vector

# Human-readable reason codes for the model's top positive contributions.
REASON_CODES: dict[str, tuple[str, str]] = {
    "amount_log": ("AMOUNT", "Payment amount is unusual"),
    "is_card": ("CARD_PAYMENT", "Card payment"),
    "hour_ist": ("TIME_OF_DAY", "Unusual time of day"),
    "instrument_count_1h": ("VELOCITY_INSTRUMENT_1H", "Many payments from this card/VPA this hour"),
    "instrument_count_24h": ("VELOCITY_INSTRUMENT_24H", "Many payments from this card/VPA today"),
    "instrument_amount_log_24h": ("SPEND_24H", "High total spend from this card/VPA today"),
    "instrument_distinct_devices_24h": ("MULTIPLE_DEVICES", "Card/VPA used on several devices"),
    "instrument_distinct_payees_24h": ("MANY_PAYEES", "Card/VPA paid many different payees"),
    "device_count_1h": ("VELOCITY_DEVICE", "Many payments from this device this hour"),
    "device_distinct_instruments_24h": ("DEVICE_MANY_CARDS", "Device used many cards or VPAs"),
    "ip_count_1h": ("VELOCITY_IP", "Many payments from this IP address"),
    "payee_distinct_payers_24h": ("PAYEE_FAN_IN", "Payee receiving from many payers"),
    "merchant_count_1h": ("MERCHANT_VOLUME", "Unusual merchant volume"),
    "new_payee": ("NEW_PAYEE", "First payment to this payee"),
    "new_device_for_instrument": ("NEW_DEVICE", "First payment from this device"),
    "geo_mismatch": ("GEO_MISMATCH", "IP country differs from card/account country"),
    "amount_to_instrument_avg": ("AMOUNT_VS_HISTORY", "Much larger than usual for this payer"),
    "log_seconds_since_last": ("RAPID_REPEAT", "Very soon after the previous payment"),
    "account_age_days": ("NEW_ACCOUNT", "Young account"),
}


@dataclass(frozen=True, slots=True)
class ReasonCode:
    code: str
    description: str
    feature: str
    contribution: float


@dataclass(frozen=True, slots=True)
class ModelScore:
    version: str
    raw: float
    calibrated: float
    reasons: tuple[ReasonCode, ...]


class ModelRuntime:
    def __init__(self, directory: Path, version: str | None = None) -> None:
        self.directory = directory
        self.metadata: dict[str, Any] = json.loads((directory / "metadata.json").read_text())
        if tuple(self.metadata["feature_names"]) != FEATURE_NAMES:
            raise ValueError("model was trained on a different feature set")
        # The registry version is authoritative; the artifact's own label is a default.
        self.version = version or str(self.metadata["version"])
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model.onnx"), options)
        self.booster = lgb.Booster(model_file=str(directory / "model.txt"))
        self._cal_x = np.asarray(self.metadata["calibration"]["x"], dtype=np.float64)
        self._cal_y = np.asarray(self.metadata["calibration"]["y"], dtype=np.float64)
        self.thresholds: dict[str, float] = self.metadata["thresholds"]

    def calibrate(self, raw: float) -> float:
        return float(np.interp(raw, self._cal_x, self._cal_y))

    def score(self, features: dict[str, float], top_k: int = 3) -> ModelScore:
        row = np.asarray([vector(features)], dtype=np.float32)
        raw = float(self.session.run(None, {"features": row})[1][0, 1])
        contributions = self.booster.predict(row, pred_contrib=True)[0][:-1]
        order = np.argsort(contributions)[::-1]
        reasons = tuple(
            ReasonCode(
                code=REASON_CODES[FEATURE_NAMES[i]][0],
                description=REASON_CODES[FEATURE_NAMES[i]][1],
                feature=FEATURE_NAMES[i],
                contribution=round(float(contributions[i]), 4),
            )
            for i in order[:top_k]
            if contributions[i] > 0
        )
        return ModelScore(self.version, raw, self.calibrate(raw), reasons)

    def decision(self, calibrated: float, method: str) -> str:
        if calibrated >= self.thresholds["block"]:
            return "block"
        if calibrated >= self.thresholds["review"]:
            # Card payments can prove possession with a challenge; UPI goes to an analyst.
            return "step_up" if method == "card" else "review"
        return "allow"

"""Population stability index and Kolmogorov-Smirnov drift checks against training baselines."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

PSI_ALERT = 0.2
PSI_WARN = 0.1


def psi(baseline: dict[str, Any], values: Sequence[float]) -> float:
    edges = np.asarray(baseline["edges"], dtype=np.float64)
    expected = np.asarray(baseline["proportions"], dtype=np.float64)
    clipped = np.clip(np.asarray(values, dtype=np.float64), edges[0], edges[-1])
    counts, _ = np.histogram(clipped, bins=edges)
    actual = counts / float(max(1, int(counts.sum())))
    expected = np.clip(expected, 1e-6, None)
    actual = np.clip(actual, 1e-6, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def ks_from_baseline(baseline: dict[str, Any], values: Sequence[float]) -> float:
    """KS statistic between the baseline's binned CDF and the sample's CDF at the same edges."""
    edges = np.asarray(baseline["edges"], dtype=np.float64)
    expected_cdf = np.concatenate([[0.0], np.cumsum(baseline["proportions"])])
    sample = np.sort(np.asarray(values, dtype=np.float64))
    actual_cdf = np.searchsorted(sample, edges, side="right") / max(1, len(sample))
    actual_cdf[0] = np.mean(sample <= edges[0]) if len(sample) else 0.0
    return float(np.max(np.abs(actual_cdf - expected_cdf)))


def drift_report(
    baselines: Mapping[str, dict[str, Any]], columns: Mapping[str, Sequence[float]]
) -> list[dict[str, Any]]:
    report = []
    for name, values in columns.items():
        if name not in baselines or len(values) == 0:
            continue
        value = psi(baselines[name], values)
        report.append(
            {
                "feature": name,
                "psi": round(value, 4),
                "ks": round(ks_from_baseline(baselines[name], values), 4),
                "status": "alert" if value >= PSI_ALERT else "warn" if value >= PSI_WARN else "ok",
                "sample_size": len(values),
            }
        )
    return report

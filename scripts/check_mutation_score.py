"""Enforce a minimum kill rate for the configured ledger and money mutants."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MINIMUM_SCORE_PERCENT = 70.0
STATS_PATH = Path("mutants/mutmut-cicd-stats.json")


def main() -> int:
    subprocess.run(["mutmut", "export-cicd-stats"], check=True)
    stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    total = int(stats["total"])
    excluded = int(stats["no_tests"]) + int(stats["skipped"])
    denominator = total - excluded
    if denominator <= 0:
        print("No runnable mutants were measured.", file=sys.stderr)
        return 2
    score = int(stats["killed"]) * 100 / denominator
    print(
        f"Mutation score: {score:.1f}% "
        f"({stats['killed']} killed / {denominator} runnable; "
        f"{stats['survived']} survived, {stats['no_tests']} without tests)"
    )
    if score < MINIMUM_SCORE_PERCENT:
        print(f"Required mutation score: {MINIMUM_SCORE_PERCENT:.1f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
